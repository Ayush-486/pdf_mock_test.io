# PDF to Mock Test

A minimal web application that converts a PDF containing MCQ questions into an online test interface.

---

## Project Structure

```
pdf_mock_test/
├── main.py              ← FastAPI backend
├── requirements.txt     ← Python dependencies
├── questions.db         ← SQLite database (auto-created on first upload)
└── static/
    ├── index.html       ← Upload page
    ├── test.html        ← Test interface
    ├── style.css        ← Shared styles
    └── test.js          ← Timer + navigation logic
```

---

## Requirements

- Python 3.10 or later (for `dict | None` and `list[dict]` syntax)
- pip

---

## Step-by-Step Setup (Windows)

### 1. Open Command Prompt or PowerShell

Navigate to the project folder:

```powershell
cd c:\projects\Antigravity\PDF\pdf_mock_test
```

### 2. Create a virtual environment

```powershell
python -m venv venv
```

### 3. Activate the virtual environment

```powershell
venv\Scripts\activate
```

### 4. Install dependencies

```powershell
pip install -r requirements.txt
```

### 5. Run the server

```powershell
uvicorn main:app --reload --host 127.0.0.1 --port 8001
```

### 6. Open the browser

Go to: [http://127.0.0.1:8001](http://127.0.0.1:8001)

---

## How to Use

1. **Upload Page** – Select a PDF file and click **Upload & Process**.
2. The backend reads the PDF, extracts MCQ questions, and stores them in SQLite.
3. You are automatically redirected to the **Test Page**.
4. **Test Page**
   - Questions are shown one at a time with radio-button options.
   - Use **Previous / Next** to navigate.
   - A **30-minute countdown timer** is shown at the top.
   - Timer turns red when ≤ 5 minutes remain.
   - Click **Submit Test** (or wait for time to expire) to see the summary.
5. **Summary** – Shows total questions, attempted, and unanswered count.

---

## PDF Format Requirements

The backend detects questions using these patterns:

| Pattern | Example |
|---------|---------|
| `<number>.` | `1. What is…` |
| `Q<number>.` | `Q1. What is…` |
| `Q.<number>` | `Q.1 What is…` |

Options must be labelled **A, B, C, D** in one of these formats:

| Pattern | Example |
|---------|---------|
| `A)` | `A) Option text` |
| `A.` | `A. Option text` |
| `(A)` | `(A) Option text` |

Pages that fail to parse are skipped automatically — the rest of the PDF is still processed.

---

## Stopping the Server

Press `Ctrl+C` in the terminal where uvicorn is running.
