/**
 * test.js – State-driven JEE CBT test engine
 *
 * Architecture:
 *  - Single global testState object owns all mutable data.
 *  - renderQuestion()  : full rebuild of question/options area (navigation only).
 *  - onOptionChange()  : minimal DOM patch – no re-render of question container.
 *  - updatePaletteBtn(): patches a single palette button (called on option change).
 *  - refreshPalette()  : patches all palette buttons (called on navigation).
 */

/* ── Auth guard ─────────────────────────────────────────────────────────── */
const _token = localStorage.getItem('ta_token');
if (!_token) {
    window.location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
}

/* ── Global state ───────────────────────────────────────────────────────── */
const testState = {
    currentIndex: 0,
    questions:    [],   // [{id, question, option_a…d, has_diagram, image_path, question_image}, …]
    answers:      {},   // { qid: 'a'|'b'|'c'|'d' }
    timeSpent:    {},   // { qid: totalSeconds }
    visited:      {},   // { qid: true }  – navigated to at least once
    marked:       {},   // { qid: true }  – marked for review
};

/* Tracks when the current question was loaded (for time accounting). */
let questionStartTime = 0;

/* Timer + attempt state */
let timerHandle = null;
let secondsLeft = parseInt(localStorage.getItem('testDuration') || '30', 10) * 60;
let attemptId   = null;

/* ── Auth helper ─────────────────────────────────────────────────────────── */
function authHeaders() {
    return { 'Authorization': 'Bearer ' + _token, 'Content-Type': 'application/json' };
}

/* ── Cached DOM refs ─────────────────────────────────────────────────────── */
const $loadingMsg        = document.getElementById('loading-msg');
const $testUI            = document.getElementById('test-ui');
const $summarySection    = document.getElementById('summary-section');
const $optionsList       = document.getElementById('options-list');
const $timerDisplay      = document.getElementById('timer-display');
const $diagramNotice     = document.getElementById('diagram-notice');
const $questionDisplay   = document.getElementById('question-display');
const $questionNumber    = document.getElementById('question-number');
const $paletteGrid       = document.getElementById('palette-grid');
const $paletteCount      = document.getElementById('palette-count');
const $prevBtn           = document.getElementById('prev-btn');
const $submitBtn         = document.getElementById('submit-btn');
const $questionScrollArea = document.getElementById('question-scroll-area');

/* Guards re-entrant submit (timer expiry + user click race). */
let isSubmitting = false;

/* ── Derived status (pure function of state) ────────────────────────────── */
function statusOf(qid) {
    const answered = !!testState.answers[qid];
    const marked   = !!testState.marked[qid];
    const visited  = !!testState.visited[qid];
    if (answered && marked) return 'answeredMarked';
    if (answered)           return 'answered';
    if (marked)             return 'marked';
    if (visited)            return 'visited';
    return 'notVisited';
}

/* ── Boot ───────────────────────────────────────────────────────────────── */
window.addEventListener('DOMContentLoaded', () => {
    loadQuestions();
    $prevBtn.addEventListener('click', onPrevious);
    document.getElementById('save-next-btn').addEventListener('click', onSaveNext);
    document.getElementById('mark-review-btn').addEventListener('click', onMarkReview);
    document.getElementById('clear-btn').addEventListener('click', onClearResponse);
    $submitBtn.addEventListener('click', () => {
        if (!isSubmitting && confirm('Are you sure you want to submit the test?')) submitTest();
    });
});

/* ── Load questions ─────────────────────────────────────────────────────── */
async function loadQuestions() {
    try {
        const res = await fetch('/api/questions');
        if (!res.ok) throw new Error('Failed to fetch questions.');
        testState.questions = await res.json();

        if (testState.questions.length === 0) {
            $loadingMsg.textContent = 'No questions found. Please upload a PDF first.';
            return;
        }

        /* Mark first question visited so palette starts correctly. */
        testState.visited[testState.questions[0].id] = true;

        await startAttempt(testState.questions.length);

        $loadingMsg.style.display = 'none';
        $testUI.style.display = 'flex';

        buildPalette();
        questionStartTime = Date.now();
        renderQuestion();
        startTimer();   /* start only after UI is ready — not during loading */
    } catch (err) {
        $loadingMsg.textContent = 'Error loading questions: ' + err.message;
    }
}

/* ── Create attempt via API ─────────────────────────────────────────────── */
async function startAttempt(totalQuestions) {
    try {
        const pdfName  = localStorage.getItem('testPdfName') || 'Unknown PDF';
        const duration = parseInt(localStorage.getItem('testDuration') || '30', 10);
        const res = await fetch('/api/attempt/start', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({ pdf_name: pdfName, total_questions: totalQuestions, duration }),
        });
        if (res.status === 401) { window.location.href = '/login?next=/test'; return; }
        if (res.ok) { attemptId = (await res.json()).attempt_id; }
    } catch (_) { /* non-fatal */ }
}

/* ── Persist answer (fire-and-forget) ──────────────────────────────────── */
function apiSaveAnswer(qid, key) {
    if (!attemptId) return;
    fetch(`/api/attempt/${attemptId}/answer`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ question_id: qid, chosen_key: key }),
    }).catch(() => {});
}

/* ── Time tracking ──────────────────────────────────────────────────────── */
function recordTimeForCurrent() {
    const q = testState.questions[testState.currentIndex];
    if (!q) return;
    const elapsed = Math.round((Date.now() - questionStartTime) / 1000);
    testState.timeSpent[q.id] = (testState.timeSpent[q.id] || 0) + elapsed;
}

/* ══════════════════════════════════════════════════════════════════════════
   PALETTE – build once, patch individually
═══════════════════════════════════════════════════════════════════════════ */

/** Build palette grid DOM once. Buttons identified by data-qidx. */
function buildPalette() {
    $paletteGrid.innerHTML = '';
    testState.questions.forEach((q, idx) => {
        const btn = document.createElement('button');
        btn.className    = 'palette-btn';
        btn.textContent  = idx + 1;
        btn.dataset.qidx = idx;
        btn.addEventListener('click', () => goToQuestion(idx));
        $paletteGrid.appendChild(btn);
    });
    if ($paletteCount) $paletteCount.textContent = `${testState.questions.length} Qs`;
    refreshPalette();
}

/** Patch a single palette button to reflect current state. */
function updatePaletteBtn(idx) {
    const btn = $paletteGrid.querySelector(`[data-qidx="${idx}"]`);
    if (!btn) return;

    const q      = testState.questions[idx];
    const status = statusOf(q.id);
    const isCur  = idx === testState.currentIndex;

    btn.className = `palette-btn q-${status}${isCur ? ' q-current' : ''}`;

    /* Remove stale dot */
    const oldDot = btn.querySelector('.palette-dot');
    if (oldDot) oldDot.remove();

    /* Add green dot for answeredMarked */
    if (status === 'answeredMarked') {
        const dot = document.createElement('span');
        dot.className  = 'palette-dot';
        dot.style.cssText = 'position:absolute;bottom:2px;right:2px;width:8px;height:8px;' +
                            'background:#22C55E;border-radius:50%;border:1px solid #fff;pointer-events:none;';
        btn.appendChild(dot);
    }
}

/** Patch all palette buttons (used after navigation). */
function refreshPalette() {
    testState.questions.forEach((_, idx) => updatePaletteBtn(idx));
}

/* ══════════════════════════════════════════════════════════════════════════
   QUESTION RENDER – only called on navigation, never on option click
═══════════════════════════════════════════════════════════════════════════ */

/**
 * @param {number|undefined} prevIdx  – the index we just left (undefined on first render)
 */
function renderQuestion(prevIdx) {
    const q   = testState.questions[testState.currentIndex];
    const num = testState.currentIndex + 1;

    /* Reset scroll to top — no position bleed between questions */
    if ($questionScrollArea) $questionScrollArea.scrollTop = 0;

    /* Header */
    $questionNumber.textContent = `Question No. ${num}`;
    $prevBtn.disabled = testState.currentIndex === 0;

    /* Clear question & options */
    $questionDisplay.innerHTML = '';
    $optionsList.innerHTML     = '';
    $diagramNotice.style.display = 'none';

    if (q.question_image) {
        /* ── Screenshot mode ───────────────────────────────────────────── */
        const img = document.createElement('img');
        img.src           = q.question_image;
        img.alt           = `Question ${num}`;
        img.style.cssText = 'max-width:100%;border:1px solid #d0d0d0;display:block;' +
                            'margin:0.5rem 0;box-shadow:0 2px 8px rgba(0,0,0,.12);';
        $questionDisplay.appendChild(img);

        ['a', 'b', 'c', 'd'].forEach(key =>
            $optionsList.appendChild(buildOptionItem(q, key, key.toUpperCase(), '', null))
        );
    } else {
        /* ── Text/fallback mode ────────────────────────────────────────── */
        const p = document.createElement('p');
        p.className = 'font-serif text-lg leading-relaxed text-gray-900 mb-4';
        p.innerHTML = `${num}. ${q.question}`;
        $questionDisplay.appendChild(p);

        if (q.image_path) {
            q.image_path.split(',').map(s => s.trim()).filter(Boolean).forEach(src => {
                const img = document.createElement('img');
                img.src           = src;
                img.alt           = 'Question diagram';
                img.style.cssText = 'max-width:100%;max-height:320px;border:1px solid #d0d0d0;' +
                    'object-fit:contain;display:block;margin:0 auto 0.5rem;' +
                    'box-shadow:0 2px 8px rgba(0,0,0,.12);';
                $questionDisplay.appendChild(img);
            });
            const cap = document.createElement('p');
            cap.style.cssText = 'font-size:0.78rem;color:#888;margin-top:0.35rem;';
            cap.textContent   = 'Diagram extracted from PDF';
            $questionDisplay.appendChild(cap);
        }

        const hasOptionImg = ['a','b','c','d'].some(l => q[`option_${l}_image`]);
        if (q.has_diagram && !q.image_path && !hasOptionImg) {
            $diagramNotice.textContent   = 'This question may reference a diagram or figure in the original PDF.';
            $diagramNotice.style.display = 'block';
        }

        [
            { key: 'a', label: 'A', text: q.option_a, image: q.option_a_image },
            { key: 'b', label: 'B', text: q.option_b, image: q.option_b_image },
            { key: 'c', label: 'C', text: q.option_c, image: q.option_c_image },
            { key: 'd', label: 'D', text: q.option_d, image: q.option_d_image },
        ].forEach(opt => {
            if (!opt.text && !opt.image) return;
            $optionsList.appendChild(buildOptionItem(q, opt.key, opt.label, opt.text || '', opt.image));
        });
    }

    /* Restore previously selected answer */
    applySelectionVisuals(testState.answers[q.id] || null);

    /* MathJax re-render — async to avoid main-thread freeze */
    if (window.MathJax) {
        MathJax.typesetPromise
            ? MathJax.typesetPromise([$questionDisplay, $optionsList]).catch(() => {})
            : MathJax.typeset([$questionDisplay, $optionsList]);
    }

    /* Palette: O(2) — only update the button we just left and the new current.
       refreshPalette() (O(n)) is only used on initial build. */
    if (prevIdx !== undefined && prevIdx !== testState.currentIndex) {
        updatePaletteBtn(prevIdx);          /* de-highlight the old current */
    }
    updatePaletteBtn(testState.currentIndex); /* highlight new current + visited */
}

/* ── Build one option <li> ──────────────────────────────────────────────── */
function buildOptionItem(q, key, labelChar, text, image) {
    const li  = document.createElement('li');
    li.style.listStyle = 'none';

    const lbl     = document.createElement('label');
    lbl.className = 'option-label';
    /* NOTE: No htmlFor — we handle clicks ourselves below so the browser
       never auto-focuses the hidden radio (which causes scroll jumps). */

    const radio       = document.createElement('input');
    radio.type        = 'radio';
    radio.className   = 'sr-only';
    radio.name        = `q_${q.id}`;
    radio.value       = key;
    radio.id          = `q${q.id}_${key}`;
    radio.tabIndex    = -1;

    const dot         = document.createElement('div');
    dot.className     = 'radio-dot';
    dot.style.cssText = 'margin-top:2px;flex-shrink:0;';

    /* ── Click on the label triggers selection directly — no browser
       focus/scroll chain, so the container never jumps. */
    lbl.addEventListener('click', (e) => {
        e.preventDefault();
        onOptionChange(q, key);
    });

    const radioWrap         = document.createElement('div');
    radioWrap.style.cssText = 'display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:2px;';
    radioWrap.appendChild(radio);
    radioWrap.appendChild(dot);

    const span       = document.createElement('span');
    span.className   = 'text-lg font-serif';
    span.innerHTML   = `(${labelChar})&nbsp;&nbsp;${text || ''}`;

    if (image) {
        const img       = document.createElement('img');
        img.src         = image;
        img.className   = 'option-diagram';
        img.alt         = `Option ${labelChar} diagram`;
        span.appendChild(img);
    }

    lbl.appendChild(radioWrap);
    lbl.appendChild(span);
    li.appendChild(lbl);
    return li;
}

/* ══════════════════════════════════════════════════════════════════════════
   OPTION SELECT – NO question re-render, minimal DOM patch only
═══════════════════════════════════════════════════════════════════════════ */

function onOptionChange(q, key) {
    /* 1. Update state immediately */
    testState.answers[q.id] = key;
    testState.visited[q.id] = true;

    /* 2. Persist (fire-and-forget) */
    apiSaveAnswer(q.id, key);

    /* 3. Patch option visuals only – no container replace */
    applySelectionVisuals(key);

    /* 4. Patch only this question's palette button */
    updatePaletteBtn(testState.currentIndex);
}

/** Toggle selected/checked classes on option labels and radio dots.
 *  Never touches the question display or outer container. */
function applySelectionVisuals(selectedKey) {
    $optionsList.querySelectorAll('.option-label').forEach(lbl => {
        const radio = lbl.querySelector('input[type="radio"]');
        const dot   = lbl.querySelector('.radio-dot');
        const isThis = radio && radio.value === selectedKey;

        lbl.classList.toggle('selected', isThis);
        if (dot)   dot.classList.toggle('checked',  isThis);
        if (radio) radio.checked = isThis;
    });
}

/* ══════════════════════════════════════════════════════════════════════════
   NAVIGATION
═══════════════════════════════════════════════════════════════════════════ */

function goToQuestion(idx) {
    if (idx < 0 || idx >= testState.questions.length) return;

    /* Account for time on departing question */
    recordTimeForCurrent();

    const prevIdx = testState.currentIndex;   /* capture before mutation */
    testState.currentIndex = idx;

    /* Mark destination as visited */
    testState.visited[testState.questions[idx].id] = true;

    /* Restart per-question timer */
    questionStartTime = Date.now();

    renderQuestion(prevIdx);
}

/* ── Footer button handlers ─────────────────────────────────────────────── */

function onPrevious() {
    if (testState.currentIndex > 0) goToQuestion(testState.currentIndex - 1);
}

function onSaveNext() {
    /* Answer was already saved in state on radio change.
       Fallback: sync from DOM in case JS event was missed. */
    const q      = testState.questions[testState.currentIndex];
    const domKey = getDOMKey(q.id);
    if (domKey && !testState.answers[q.id]) {
        testState.answers[q.id] = domKey;
        apiSaveAnswer(q.id, domKey);
    }
    if (testState.currentIndex < testState.questions.length - 1) {
        goToQuestion(testState.currentIndex + 1);
    }
}

function onMarkReview() {
    const q = testState.questions[testState.currentIndex];
    testState.marked[q.id]  = true;
    testState.visited[q.id] = true;

    /* Patch palette immediately before navigation */
    updatePaletteBtn(testState.currentIndex);

    if (testState.currentIndex < testState.questions.length - 1) {
        goToQuestion(testState.currentIndex + 1);
    }
}

function onClearResponse() {
    const q = testState.questions[testState.currentIndex];
    delete testState.answers[q.id];

    /* Clear visuals without re-render */
    applySelectionVisuals(null);

    /* Patch palette: answered → visited, answeredMarked → marked */
    updatePaletteBtn(testState.currentIndex);
}

/** Read currently checked radio from the DOM (fallback). */
function getDOMKey(qid) {
    const el = $optionsList.querySelector(`input[name="q_${qid}"]:checked`);
    return el ? el.value : null;
}

/* ══════════════════════════════════════════════════════════════════════════
   TIMER
═══════════════════════════════════════════════════════════════════════════ */

function startTimer() {
    updateTimerDisplay();
    timerHandle = setInterval(() => {
        secondsLeft--;
        if (secondsLeft <= 0) {
            secondsLeft = 0;
            updateTimerDisplay();
            clearInterval(timerHandle);
            alert('Time is up! The test will be submitted automatically.');
            submitTest();
        } else {
            updateTimerDisplay();
        }
    }, 1000);
}

function updateTimerDisplay() {
    const m = Math.floor(secondsLeft / 60).toString().padStart(2, '0');
    const s = (secondsLeft % 60).toString().padStart(2, '0');
    $timerDisplay.textContent = `${m}:${s}`;
    $timerDisplay.style.color = secondsLeft <= 300 ? '#ff4444' : '';
}

/* ══════════════════════════════════════════════════════════════════════════
   SUBMIT
═══════════════════════════════════════════════════════════════════════════ */

async function submitTest() {
    if (isSubmitting) return;
    isSubmitting = true;
    if ($submitBtn) $submitBtn.disabled = true;

    /* Account for time on last question */
    recordTimeForCurrent();
    clearInterval(timerHandle);

    /* Sync any answer that didn't land in state yet (rare race condition) */
    const q      = testState.questions[testState.currentIndex];
    const domKey = q ? getDOMKey(q.id) : null;
    if (domKey && !testState.answers[q.id]) {
        testState.answers[q.id] = domKey;
        /* No apiSaveAnswer here — submit body handles it without the race */
    }

    if (attemptId) {
        try {
            const res = await fetch(`/api/attempt/${attemptId}/submit`, {
                method: 'POST',
                headers: authHeaders(),
                body: JSON.stringify({
                    answers:    testState.answers,
                    time_spent: testState.timeSpent,
                }),
            });
            if (res.status === 401) {
                window.location.href = '/login?next=/test';
                return;
            }
        } catch (_) {}
        window.location.href = `/result?id=${attemptId}`;
        return;
    }

    /* Fallback: no attempt tracked — show inline summary */
    const total      = testState.questions.length;
    const attempted  = Object.keys(testState.answers).length;
    const unanswered = total - attempted;

    $testUI.style.display     = 'none';
    $loadingMsg.style.display = 'none';
    document.getElementById('timer-wrap').style.display = 'none';
    $summarySection.style.display = 'block';

    document.getElementById('s-total').textContent      = total;
    document.getElementById('s-attempted').textContent  = attempted;
    document.getElementById('s-unanswered').textContent = unanswered;

    const listEl = document.getElementById('s-question-list');
    listEl.innerHTML = '';
    testState.questions.forEach((q, idx) => {
        const li      = document.createElement('li');
        li.style.cssText = 'padding:0.25rem 0;border-bottom:1px solid #eee;';
        const answered = testState.answers[q.id];
        li.textContent = answered
            ? `✅ Q${idx + 1}: Answered: ${answered.toUpperCase()}`
            : `⬜ Q${idx + 1}: Unanswered`;
        li.style.color = answered ? '#16a34a' : '#dc2626';
        listEl.appendChild(li);
    });
}
