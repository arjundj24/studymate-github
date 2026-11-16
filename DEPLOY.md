# Deploy StudyMate for free (GitHub + Render)

**Read this first:** GitHub Pages hosts *static* sites (HTML files). StudyMate is a Python server — it reads PDFs, runs OCR and streams AI answers — so GitHub Pages cannot run it. GitHub stores the code; **Render** runs it. Both are free and neither needs a credit card.

Two stages:

1. **Put the code on GitHub** (5 minutes)
2. **Point Render at the repo** (5 minutes)

---

## Stage 1 — Put the code on GitHub

### 1.1 Create an account
Go to <https://github.com> → **Sign up**. Verify your email.

### 1.2 Create the repository
1. Click the **+** (top right) → **New repository**
2. **Repository name:** `studymate`
3. **Public** ← required for free Render deploys
4. Leave everything else unticked → **Create repository**

### 1.3 Upload the files
On the empty repo page click **uploading an existing file** (or go to `https://github.com/<your-name>/studymate/upload/main`).

Drag **the contents of the `studymate-repo` folder** — not the folder itself:

```
app/                 ← drag this folder
requirements.txt
render.yaml
Dockerfile
Procfile
README.md
DEPLOY.md
LICENSE
.gitignore
```

Then click **Commit changes**.

**Check:** the repo's front page must show `app`, `requirements.txt` and `render.yaml` at the top level. If you see a single `studymate-repo` folder instead, move the files up (GitHub lets you drag them again).

> `.gitignore` is hidden in some file pickers. Enable "show hidden files" or just skip it — it is not required to deploy.

### Uploading from a computer without the folder?
Each file is in this workspace. Download the zip from the chat, unzip it, and drag the `app` folder plus the loose files.

---

## Stage 2 — Deploy on Render

### 2.1 Create an account
<https://render.com> → **Get Started** → **GitHub** → authorise. No card is asked for on the free plan.

### 2.2 Use the blueprint
1. Dashboard → **New +** → **Blueprint**
2. Choose your `studymate` repository → **Connect**
3. Render reads `render.yaml` and shows a service called **studymate** on the **Free** plan → **Apply**

*(If Blueprint is unavailable, choose **New + → Web Service** instead: Build Command `pip install -r requirements.txt`, Start Command `uvicorn main:app --app-dir app --host 0.0.0.0 --port $PORT`, Health Check Path `/healthz`, Plan **Free**.)*

### 2.3 Wait for the first build
About 3–6 minutes. The log ends with:

```
INFO:     Uvicorn running on http://0.0.0.0:10000
```

Render then shows a URL like:

```
https://studymate-abc1.onrender.com
```

That is your public site.

---

## Stage 3 — Make it usable

Open the URL. The page loads with no AI attached — that is correct: on the web version **each visitor brings their own key**, which is free and needs no card.

Click **Add AI key** (top right):

| Field | Value |
|---|---|
| Provider | **Google Gemini** |
| API key | from <https://aistudio.google.com/apikey> (free, no card) |
| Model | leave empty → `gemini-2.5-flash` |

Press **Test this key**, then **Save for this browser only**.

> **Groq** also works free and needs no card: <https://console.groq.com/keys> (model `llama-3.3-70b-versatile`).

Test with a file: **Add material** → any PDF → ask something. First answer takes 2–5 s; after 15 idle minutes the service sleeps, so the first request after that takes ~30 s.

---

## What to expect from the free tier

| Reality | Detail |
|---|---|
| Sleeps when idle | after 15 min with no visitors; next request takes ~30 s to wake |
| **No permanent storage** | uploads live in the container. When it restarts, the library resets. Fine for demos, not for keeping notes |
| 512 MB RAM | OCR (about 300 MB) fits, but if the service ever restarts on its own set `STUDYMATE_OCR=0` in **Environment** |
| 750 hours/month | one always-awake free service ≈ 730 h, so it fits exactly — but two services would not |
| No card, no charge | the free plan never bills you |

**Want uploads to survive restarts?** Run StudyMate on your own laptop and share it through a Cloudflare tunnel (the Windows setup you already had). Documents then live on your disk, the AI runs on your CPU, and nothing costs anything.

---

## Updating the site later

1. Edit a file on GitHub (pencil icon) or upload a new copy → **Commit changes**
2. Render redeploys automatically in ~2 minutes

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Build fails: `No module named ...` | `requirements.txt` not at the repo root | it must sit next to `app/` |
| Build fails on `onnxruntime` | platform wheel missing | set env `STUDYMATE_OCR=0`, or remove `rapidocr-onnxruntime` from `requirements.txt` |
| Page loads, answers never come | no key attached | **Add AI key** with a Gemini key |
| `502` after a long idle | free instance was asleep | reload, wait 30 s |
| Uploads vanish | free tier has no disk | expected — see the table above |
| "This model cannot read pictures" | the chosen model is text-only | the app now answers from OCR text instead; upgrade the model if you need true vision |

## Optional: give everyone AI without their own key

Save a **site key** on the server (Settings → bottom panel → *Site key*, with an owner passcode). On a public deploy, prefer an environment variable instead, so the key is not tied to one browser session:

```
STUDYMATE_LLM_PROVIDER = gemini
STUDYMATE_LLM_KEY      = <your free Gemini key>
STUDYMATE_DAILY_LIMIT  = 250
```

Delete the variable to revoke it. Never commit a key to the repo.
