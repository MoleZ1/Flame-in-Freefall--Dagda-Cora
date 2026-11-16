# Flame in Freefall

**Ask 502 NASA microgravity fire experiments what would happen in your spacecraft cabin.**

*NASA Space Apps Challenge 2026 · Team Dagda-Cora*

Flame in Freefall is a web application built on NASA Physical Sciences Informatics (PSI) combustion data. It finds the experiments closest to a set of conditions (oxygen, pressure, gravity, fuel) and uses two open AI models, running locally, to explain what those experiments show. Every answer cites the NASA tests it came from, and any sentence the data does not support is removed before it is shown.

> **Status:** hackathon prototype. It is a tool for exploring prior evidence, **not** a fire-safety certification.

---

## Contents

1. [Why this project](#1-why-this-project)
2. [What the app does](#2-what-the-app-does)
3. [How it works](#3-how-it-works)
4. [The data](#4-the-data)
5. [Repository structure](#5-repository-structure)
6. [Infrastructure: the Google Cloud VM](#6-infrastructure-the-google-cloud-vm)
7. [Software stack and model weights](#7-software-stack-and-model-weights)
8. [Installation, step by step](#8-installation-step-by-step)
9. [Configuration reference](#9-configuration-reference)
10. [API reference](#10-api-reference)
11. [Testing](#11-testing)
12. [Troubleshooting](#12-troubleshooting)
13. [Limitations and known issues](#13-limitations-and-known-issues)
14. [Use of AI](#14-use-of-ai)
15. [Team](#15-team)
16. [License](#16-license)
17. [Sources](#17-sources)

---

## 1. Why this project

- **Fire behaves differently in space.** Without gravity, hot gas does not rise, so a flame does not stretch upward; it can burn as a sphere.
- **Future cabins may carry more oxygen.** The ISS uses about 101.3 kPa and 21 % O2. A proposed exploration atmosphere for Moon and Mars missions is about 56.5 kPa and 34 % O2. More oxygen makes it easier for a fire to start and spread.
- **The evidence exists but is scattered.** NASA has run hundreds of microgravity combustion tests, but the results sit in separate tables, reports and PDFs that are slow to search.

Flame in Freefall puts that evidence in one database and lets anyone question it in plain language.

---

## 2. What the app does

The app has three pages and an assistant.

| Page | What you can do |
| --- | --- |
| **Home** (`/`) | See each NASA investigation as a card: objective, platform, center, dates, fuels, and counts of experiments, datasets and findings. Download a **PDF summary** of any investigation. Check a crew atmosphere (ISS or Exploration) against the closest tests. Read key findings from the reports. |
| **Add investigation** (`/insertion`) | Upload a new study (CSV, Excel, PDF, TXT, ZIP or JSON) and describe it. It is validated against the database schema and added right away, marked as a user upload. |
| **Suggestions** (`/suggestion`) | Pick an investigation and see it in context: affinity with each NASA investigation, your ranges compared with the database, a scatter plot of any two variables, and notes, per-variable comments and recommendations written by the assistant. |
| **Assistant** (button on every page) | Ask questions about the database in any language. Answers are in the question's language, cite PSI IDs, and include a warning when a described cabin looks risky. |

### Example: a real result

For an exploration cabin at **56.5 kPa and 34 % O2**, the app finds **11** NASA tests within ±3 O2 percentage points and a factor of 1.42 in pressure:

| Outcome | Count |
| --- | --- |
| Ignited | 8 of 11 |
| Went out on their own | 4 of 9 |
| Burned until the fuel was cut | **5 of 9** |

The nearest test is from PSI-159, at 50.7 kPa and 34.1 % O2. Because more than 25 % of the close tests kept burning above 21 % O2, the assistant raises a warning (see [3.5](#35-scenario-analysis-and-the-cabin-alert)).

---

## 3. How it works

### 3.1 Architecture

Everything runs on one machine. No service is exposed to the internet.

```
 Browser ──HTTP──►  Flask app  (127.0.0.1:5000)
                     │
                     ├── app/            pages + JSON APIs
                     └── motor_psi/      ranking engine
                           │
              ┌────────────┴─────────────┐
              ▼                          ▼
        MongoDB (127.0.0.1:27017)   Ollama (127.0.0.1:11434)
        database: psi_science       ├── qwen3-embedding:0.6b   "Qwen #1" — measures
        + GridFS bucket raw_files   └── qwen3.5:9b-q8_0         "Qwen #2" — writes
                                         (both on one NVIDIA V100 16 GB)
```

### 3.2 Path of one question

1. The browser sends the question to Flask (`POST /api/assistant`).
2. `motor_psi` reads the experiments from MongoDB and computes a **numeric affinity** from oxygen, pressure, gravity and fuel.
3. **Qwen #1** (`/api/embed`) turns the question into a 1024-dimension vector and compares it with the stored experiment vectors. This gives the **semantic affinity**.
4. The two scores are combined, ranked, and the top **8** experiments become the *facts*.
5. **Qwen #2** (`/api/chat`, thinking disabled, JSON-schema output) writes the answer from those facts only.
6. A **validator** removes every sentence that mentions an ID or a number not found in the facts, and counts how many it removed.
7. Flask returns the answer, the sources, the scenario analysis and, if triggered, the alert.

### 3.3 Affinity and ranking

**Combined affinity**

```
A_total = α · A_num + (1 − α) · A_sem        α = ALFA_NUMERICA = 0.6
```

- `A_num`: closeness in physical conditions (oxygen fraction, pressure on a log scale, gravity environment, fuel).
- `A_sem`: cosine similarity between Qwen3-Embedding vectors of the question and of each experiment's text. Queries carry an instruction prefix because the model is instruction-aware.

**Ranking**

- **Reciprocal rank fusion (RRF)** merges the numeric and semantic rankings, so one strong signal cannot hide the other.
- **Maximal marginal relevance (MMR)**, with `λ = LAMBDA_MMR = 0.7`, then picks the final 8. It trades relevance for diversity, so the facts do not all come from one investigation.

### 3.4 Writing and checking

- The writer model receives a system prompt with four rules: interpret the user's conditions, cite IDs, use numbers only from FACTS, and answer in the question's language.
- Output is constrained by a JSON schema (`answer`, `recommendations`, …).
- `narrador.validar` and the assistant's sentence filter drop any sentence with an unsupported ID or number. The Suggestions page shows how many statements were removed.

### 3.5 Scenario analysis and the cabin alert

When the user describes an atmosphere, the assistant looks for experiments inside two tolerance windows: **1.5σ** and **2.5σ**. The scales are σ = 0.03 for the O2 fraction (3 percentage points) and σ = 0.35 for log-pressure (a factor of about 1.42). The Home readout uses a 1σ window: ±3 O2 points and ×1.42 pressure.

For those tests it reports:
- the conditions range
- the ignition rate (`hot_ignition`)
- how they ended (`termination_mode`: `self_extinguished` or `flow_terminated`, meaning they kept burning until the flow was cut)
- the oxygen trend of the kept-burning rate

**Alert rule.** The rule is deterministic; the model only explains it:

```
alert  if  O2 > 21 %  and  (close tests that kept burning) / (close tests with a recorded ending) ≥ 25 %
```

Reference atmospheres built into the app:

| Name | Pressure | O2 |
| --- | --- | --- |
| ISS | 101.3 kPa | 21 % |
| Exploration | 56.5 kPa | 34 % |

### 3.6 Embedding index

- The vectors are cached in `INDICE_DIR` (`vectores.npy` + `meta.json`).
- At start-up the app runs an **incremental update**. It reuses vectors by record id and model, and embeds only new records, such as a fresh upload.
- Changing `EMB_MODEL` invalidates the cache, so the index must be rebuilt.

---

## 4. The data

### 4.1 Sources

All scientific data comes from **NASA Physical Sciences Informatics (PSI)**, https://psi.nasa.gov.

| PSI ID | Investigation | Environment | Center | Period | Experiments in our DB |
| --- | --- | --- | --- | --- | --- |
| PSI-159 | ACME CFI-G: Cool Flames Investigation with Gases | Microgravity, International Space Station | Glenn Research Center | Released 2025 | 333 |
| PSI-142 | Quantitative Studies of Cool Flame Transitions at Radiation/Stretch Extinction Using Counterflow Flames | Ground | Glenn Research Center | 2016–2021 | 124 |
| PSI-117 | Effect of External Thermo-Convective Perturbation on Cool Flame Dynamics | Ground | Glenn Research Center | 2017–2022 | 45 |
| | | | | **Total** | **502** |

### 4.2 How we selected the files

PSI investigations ship many files: tables, reports, logs, camera data, videos and image archives of several GB. We chose files with **question-driven triage**:

1. **Ask.** For each investigation, write research questions from its official objective. For example: *What conditions put the flame out?*
2. **Check.** Map which file answers each question.
3. **Prioritize.**
   - **High:** structured tables, such as `CFI-G_Normal Flames.csv`, `CFI-G_Inverse Flames.csv`, `*_data.csv`, `*_mfcSetpoints.csv`, plus the final reports.
   - **Medium:** logs and test documentation.
   - **Selective:** videos (`.ts`, `.mp4`) and multi-GB ZIP image archives. These were set aside.

This priority decided **what went into the database**. It is not a weight in the ranking.

### 4.3 Database

Database: **`psi_science`** on MongoDB. Collections are created by `db/init_db.js` with strict `$jsonSchema` validators, and data is loaded by `db/load_seed.js` from `SEED_DIR`.

| Collection | Contents |
| --- | --- |
| `investigations` | One document per PSI investigation: title, acronym, type, platform, center, dates, objective, DOI, contacts, keywords |
| `experiments` | One document per test: investigation, fuel, O2 fraction, pressure, gravity, outcomes (`hot_ignition`, `termination_mode`, …) |
| `measurements_metadata` | Which datasets exist and where they are stored (`data_role`, `availability`, location) |
| `documents` | Reports, plus text chunks with page numbers for retrieval |
| findings | Key results from the reports (IDs like `FND:PSI-159:001`), shown on Home and cited in recommendations |
| `chat_sessions`, `relevance_judgments` | Assistant conversations and feedback |
| `raw_files.files` / `raw_files.chunks` | GridFS bucket for original uploaded files |

Run this to see the exact list on your instance:

```bash
mongosh "mongodb://localhost:27017/psi_science" --quiet --eval 'db.getCollectionNames()'
```

**Validator rules to know**

- `investigation_id` must match `^PSI-[0-9]+$`.
- `schema_version` and `extraction{pipeline, pipeline_version, review_status}` are required.
- `record_type` and `data_role` only accept their enum values.
- A document that breaks a rule is rejected with `WriteError` code 121. The API turns that into an HTTP 422 with a readable message and rolls back the partial upload.

**User uploads** (from the Add investigation page)

- IDs are assigned automatically from `PSI-9001` upward, so uploads never collide with NASA IDs.
- Each upload is marked `origin: "user_upload"`, `pipeline: "flame_app.upload"`, `schema_version: "0.1"`.
- Files are stored in GridFS with `file_id = "sha256:<hash>"`.
- An upload is excluded from its own comparison. Only user uploads can be deleted (`DELETE /api/investigations/<id>`).

---

## 5. Repository structure

```
Flame-in-Freefall--Dagda-Cora/
├── main.py                  # entry point: loads config.env, creates the Flask app, serves on :5000
├── config.env.example       # copy to config.env and edit (config.env itself is not committed)
├── requirements.txt
├── demo.py                  # command-line demo of the engine
├── app/                     # Flask application
│   ├── __init__.py          # app factory: Mongo client, Motor, blueprints, 200 MB upload limit
│   ├── config.py            # MONGO_URI, MONGO_DB from the environment
│   ├── pages.py             # HTML routes: /, /suggestion, /insertion
│   ├── chat_api.py          # /api/chat/* (interpret, answer, feedback)
│   ├── investigations_api.py# /api/investigations (upload, list, analysis, annotations, delete)
│   ├── assistant_api.py     # /api/assistant (grounded Q&A, scenario analysis, alert)
│   ├── dashboard_api.py     # /api/dashboard, PDF summaries, file downloads
│   ├── templates/           # index.html, suggestion.html, insertion.html
│   └── static/
│       ├── css/             # styles.css, suggestion.css, assistant.css
│       └── js/              # index.js, suggestion.js, insertion.js, assistant.js
├── motor_psi/               # ranking and writing engine
│   ├── __init__.py          # Motor: interpretar(), afinidad(), recargar()
│   ├── base.py              # loads investigations/experiments from MongoDB (obtener_base, reiniciar_base)
│   ├── semantica.py         # IndiceSemantico: embeddings, cache, incremental update
│   ├── servicio.py          # Servicio: numeric + semantic affinity, RRF, MMR
│   ├── narrador.py          # prompts for Qwen #2 and the validator (validar)
│   └── indexar.py           # CLI: python -m motor_psi.indexar --mongo
├── db/
│   ├── init_db.js           # creates collections + $jsonSchema validators
│   ├── load_seed.js         # loads the PSI records from SEED_DIR
│   └── schemas/             # validator definitions and seed data
└── indice_embeddings/       # generated vector cache (not committed)
```

> Folder names in `motor_psi` are in Spanish (`base`, `semantica`, `servicio`, `narrador`, `indexar`); the app's UI and API are in English.

---

## 6. Infrastructure: the Google Cloud VM

We developed and ran everything on one Compute Engine VM.

| Parameter | Value |
| --- | --- |
| Provider | Google Cloud Compute Engine |
| Instance name | `flama-fria` |
| Operating system | Debian 12 "bookworm", x86_64 |
| GPU | 1 × NVIDIA Tesla V100-SXM2, 16 GB VRAM |
| Machine type | `[fill in, e.g. n1-standard-8]` (V100 GPUs attach to N1 machine types) |
| vCPUs / RAM | `[fill in]` |
| Boot disk | `[fill in: type and size]` (needs ≥ 30 GB free: ~11 GB model weights + database + Python env) |
| Zone | `[fill in]` |
| User | `root` |
| Open ports | SSH only. Flask, MongoDB and Ollama listen on `127.0.0.1`; the app is reached through an SSH tunnel |

Fill in the placeholders with the real values:

```bash
gcloud compute instances describe flama-fria --zone=<ZONE> \
  --format="yaml(machineType,guestAccelerators,disks,zone,scheduling)"
```

### Recreate a similar VM

```bash
gcloud compute instances create flama-fria \
  --zone=<ZONE> \
  --machine-type=<N1_MACHINE_TYPE> \
  --accelerator=type=nvidia-tesla-v100,count=1 \
  --maintenance-policy=TERMINATE \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=<SIZE>GB
```

GPU VMs must use `--maintenance-policy=TERMINATE`. V100 availability depends on the zone.

### Lessons learned

- **Stop the VM when you are not using it.** GPU VMs are billed while running.
- **Keep the disk.** If the boot disk is deleted with the VM, everything is lost, including ~11 GB of model weights, the database and the index. Use a persistent disk, or plan to redo [section 8](#8-installation-step-by-step) after each new VM.
- **Open the app through a tunnel**, not a public firewall rule: `gcloud compute ssh flama-fria -- -L 5000:localhost:5000`.

---

## 7. Software stack and model weights

### 7.1 Stack

| Layer | Technology |
| --- | --- |
| Web | Flask (Python), Jinja templates, plain JavaScript, hand-drawn SVG charts, CSS |
| Database | MongoDB Community (database `psi_science`), `$jsonSchema` validators, GridFS |
| AI serving | Ollama (systemd service), local GPU inference |
| Python libraries | pymongo, numpy, pandas, scikit-learn, requests, openpyxl, pdfplumber, reportlab |
| Tooling | mongosh, tmux, gcloud CLI |

### 7.2 The two models

| Role | Ollama tag | Parameters | Quantization | Size on disk | Notes |
| --- | --- | --- | --- | --- | --- |
| **Qwen #1, measures** | `qwen3-embedding:0.6b` | ~0.6 B | Q8_0 | ~639 MB | 28 layers, up to 32K tokens per input, **1024-dim** vectors, instruction-aware |
| **Qwen #2, writes** | `qwen3.5:9b-q8_0` | 9 B, dense | Q8_0 (8-bit) | ~10 GB in `ollama list` (the Ollama library lists 11 GB) | Native context 262,144 tokens; we cap it at **16,384** |

- With both models loaded and a 16K context, the V100 uses about **13–14 GB of 16 GB**.
- **Q8_0** is practically lossless compared with full precision.
- **Smaller GPU (8 GB)?** Use `qwen3.5:9b` (Q4, ~6.6 GB) and set `QWEN_MODEL=qwen3.5:9b`. The embedding model stays the same, so there is no need to re-index.

Qwen3.5-9B sizes by quantization (GGUF):

| Quantization | Size | Typical GPU |
| --- | --- | --- |
| BF16 | 18.41 GB | Does not fit in 16 GB |
| **Q8_0 (ours)** | 9.80 GB | 12–16 GB |
| Q6_K | 7.96 GB | 10–12 GB |
| Q5_K_M | 7.11 GB | 8–10 GB |
| Q4_K_M | 6.17 GB | 8 GB or CPU |

---

## 8. Installation, step by step

All commands are for **Debian 12 x86_64** with an NVIDIA GPU, run over SSH. Drop `sudo` if you are `root`. For Ubuntu 22.04/24.04 the commands are the same except the MongoDB repository line. On Windows, use WSL2 with Ubuntu.

### 8.1 GPU driver and base packages

```bash
cat /etc/os-release          # Debian GNU/Linux 12 (bookworm)
lspci | grep -i nvidia       # the GPU must appear
nvidia-smi                   # if "command not found", install the driver:

# Google Cloud driver installer (Ollama needs the driver, not the CUDA toolkit)
sudo apt update && sudo apt install -y python3 curl
curl -L https://storage.googleapis.com/compute-gpu-installation-us/installer/latest/cuda_installer.pyz \
     --output cuda_installer.pyz
sudo python3 cuda_installer.pyz install_driver   # reboots; SSH back in and run it again

sudo apt install -y git curl gnupg ca-certificates python3 python3-venv python3-pip tmux
```

### 8.2 Clone

```bash
cd /root
git clone https://github.com/<user>/Flame-in-Freefall--Dagda-Cora.git
cd Flame-in-Freefall--Dagda-Cora
export PROJECT="$(pwd)"
```

### 8.3 Ollama and the models

```bash
curl -fsSL https://ollama.com/install.sh | sh
systemctl status ollama --no-pager          # active (running)

ollama pull qwen3-embedding:0.6b             # ~639 MB
ollama pull qwen3.5:9b-q8_0                  # ~10 GB
ollama list
```

Configure the service. Shell `export`s do **not** reach a systemd service.

```bash
sudo systemctl edit ollama.service
```

```ini
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_CONTEXT_LENGTH=16384"
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
systemctl show ollama --property=Environment

# pre-load both models onto the GPU
curl -s localhost:11434/api/embed -d '{"model":"qwen3-embedding:0.6b","input":"warmup"}' >/dev/null
curl -s localhost:11434/api/chat  -d '{"model":"qwen3.5:9b-q8_0","messages":[{"role":"user","content":"hi"}],"think":false,"stream":false}' >/dev/null
ollama ps        # both at 100% GPU, UNTIL = Forever
```

### 8.4 MongoDB and the data

```bash
curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc | \
  sudo gpg -o /usr/share/keyrings/mongodb-server-8.0.gpg --dearmor
echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] http://repo.mongodb.org/apt/debian bookworm/mongodb-org/8.0 main" | \
  sudo tee /etc/apt/sources.list.d/mongodb-org-8.0.list
sudo apt update && sudo apt install -y mongodb-org
sudo systemctl enable --now mongod

cd "$PROJECT"
cp config.env.example config.env        # then edit paths (section 9.2)
set -a; source config.env; set +a       # exports SEED_DIR for load_seed.js

mongosh "mongodb://localhost:27017/psi_science" db/init_db.js
mongosh "mongodb://localhost:27017/psi_science" db/load_seed.js
mongosh "mongodb://localhost:27017/psi_science" --quiet \
  --eval 'print(db.investigations.countDocuments(), db.experiments.countDocuments())'
# expect: 3 502
```

The URI **must** end in `/psi_science`. Without it, `mongosh` loads into the `test` database and the app sees an empty database.

### 8.5 Python environment

```bash
cd "$PROJECT"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Always use `python -m pip` inside the venv. A bare `pip` can install into the system Python instead.

### 8.6 Build the index and run

```bash
set -a; source config.env; set +a
python -m motor_psi.indexar --mongo      # writes vectores.npy + meta.json to INDICE_DIR

tmux new -s flame
python main.py                           # http://127.0.0.1:5000  (detach: Ctrl-b d)
```

From your laptop:

```bash
gcloud compute ssh flama-fria -- -L 5000:localhost:5000
# open http://localhost:5000
```

**Start order after a reboot:** MongoDB, then Ollama, then Flask. Flask must start last because the engine reads the database and calls the embedding model at start-up. MongoDB and Ollama start automatically as services.

---

## 9. Configuration reference

### 9.1 Ollama server (systemd override)

| Variable | Our value | Default | Why |
| --- | --- | --- | --- |
| `OLLAMA_HOST` | `127.0.0.1:11434` | same | Only this machine can reach the models |
| `OLLAMA_KEEP_ALIVE` | `-1` | 5 minutes | Keep models in VRAM; avoids a ~1 min reload after a pause |
| `OLLAMA_MAX_LOADED_MODELS` | `2` | 3 × GPUs | Both Qwens resident together |
| `OLLAMA_CONTEXT_LENGTH` | `16384` | 4096 (newer versions scale with VRAM) | Room for 8 facts + answer. Lower to `8192` if memory is short |

After a change, run `sudo systemctl daemon-reload && sudo systemctl restart ollama`.

### 9.2 `config.env` (application)

`main.py` loads this file into the environment before the app is created. For shell tools (`indexar`, `demo.py`, `load_seed.js`), run `set -a; source config.env; set +a` first.

| Variable | Our value | Meaning | When to change |
| --- | --- | --- | --- |
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB server | Remote DB or auth |
| `MONGO_DB` | `psi_science` | Database name | — |
| `EMB_BACKEND` | `ollama` | Embedding provider | — |
| `EMB_URL` | `http://localhost:11434` | Ollama URL | Ollama on another host |
| `EMB_MODEL` | `qwen3-embedding:0.6b` | Qwen #1 | Changing it requires re-indexing |
| `EMB_USA_INSTRUCCION` | `1` | Prefix queries with a task instruction | Keep 1 |
| `INDICE_DIR` | `<project>/indice_embeddings` | Vector cache folder | Must be writable |
| `LLM_BACKEND` | `ollama` | Writer provider | — |
| `QWEN_URL` | `http://localhost:11434` | Ollama URL | — |
| `QWEN_MODEL` | `qwen3.5:9b-q8_0` | Qwen #2 | `qwen3.5:9b` on 8 GB GPUs |
| `QWEN_TIMEOUT` | `120` | Seconds per request | Raise on CPU-only |
| `QWEN_NO_THINK` | `1` | Sends `think:false`: faster, clean JSON | Keep 1 |
| `QWEN_TEMPERATURA` | `0.7` | Sampling temperature | 0.3–0.5 for more literal text |
| `QWEN_TOP_P` | `0.8` | Nucleus sampling | — |
| `QWEN_TOP_K` | `20` | Candidate tokens per step | — |
| `QWEN_PRESENCE_PENALTY` | `1.5` | Discourages repetition | Toward 2 if it repeats |
| `ALFA_NUMERICA` | `0.6` | Weight of numeric vs semantic affinity | Higher: conditions matter more |
| `LAMBDA_MMR` | `0.7` | Relevance vs diversity in the top 8 | Lower: more varied sources |
| `SEED_DIR` | `<project>/db/schemas` | Where `load_seed.js` reads data | — |

The sampling values (0.7 / 0.8 / 20 / 1.5) follow Qwen's recommendation for non-thinking mode.

### 9.3 Flask

| Setting | Value | Where |
| --- | --- | --- |
| Host / port | `127.0.0.1:5000` | `main.py` |
| Max request size | 200 MB (the upload page allows up to 50 MB per file) | `app/__init__.py` (`MAX_CONTENT_LENGTH`) |
| Engine start | `Motor(actualizar_indice=True)`; errors are stored and shown instead of crashing the app | `app/__init__.py` |

---

## 10. API reference

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/`, `/suggestion`, `/insertion` | Pages |
| GET | `/api/dashboard` | Investigation cards, experiment points (pressure, O2, outcomes), findings, references |
| GET | `/api/dashboard/<inv_id>/summary.pdf` | PDF summary of an investigation (ReportLab; returns 501 if it is missing) |
| GET | `/api/files/<doc_id>` | Download an original file from GridFS, or redirect to its external URL/DOI |
| POST | `/api/investigations` | Upload an investigation (multipart form + files) |
| GET | `/api/investigations` | List investigations |
| GET | `/api/investigations/<id>/analysis` | Affinity, ranges and scatter data for one investigation |
| POST | `/api/investigations/<id>/annotations` | Assistant summary, per-variable notes, recommendations (cached in the investigation) |
| DELETE | `/api/investigations/<id>` | Delete a user upload (NASA records cannot be deleted) |
| POST | `/api/assistant` | `{message, history, context:{page, investigation_id}}` → `answer`, `recommendations`, `sources`, `scenario`, `alert` |
| POST | `/api/chat/interpret`, `/api/chat/answer`, `/api/chat/feedback` | Older three-step chat flow, kept for compatibility |

---

## 11. Testing

```bash
# 1. Qwen #1 returns 1024 dimensions
curl -s localhost:11434/api/embed -d '{"model":"qwen3-embedding:0.6b","input":"test"}' \
  | python3 -c "import sys,json;print(len(json.load(sys.stdin)['embeddings'][0]))"

# 2. Database → Flask
curl -s localhost:5000/api/dashboard | python3 -m json.tool | head -40

# 3. Full pipeline: database → Qwen #1 → Qwen #2 → validator
curl -s localhost:5000/api/assistant -H "Content-Type: application/json" \
  -d '{"message":"My cabin is at 34% oxygen and 56.5 kPa. Is a fire dangerous?","history":[],"context":{"page":"index"}}' \
  | python3 -m json.tool

# 4. PDF
curl -s -o PSI-159.pdf localhost:5000/api/dashboard/PSI-159/summary.pdf && file PSI-159.pdf
```

Checklist:

- [ ] `nvidia-smi` shows the GPU
- [ ] `ollama ps` shows both models at 100% GPU
- [ ] 502 experiments in `psi_science`
- [ ] `INDICE_DIR` has `vectores.npy` and `meta.json`
- [ ] `/api/dashboard` returns 3 NASA investigations
- [ ] `/api/assistant` cites PSI IDs and warns for 34 % O2 at 56.5 kPa

> A direct `curl` to Qwen #2 has **no database context** and will invent answers. Asked about "PSI-117", ours described an android. Grounded answers only come through the app.

---

## 12. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `EMB_BACKEND no configurado` | `config.env` not loaded in this shell | `set -a; source config.env; set +a` |
| `ValueError: empty vocabulary` / "The database is empty…" | Engine started with no data (wrong URI, or seed loaded into `test`) | Check `config.env`, reload with `/psi_science`, restart Flask |
| `ModuleNotFoundError: app.config` | `app/config.py` missing | Restore it |
| 404 on `/api/...` | An old server is still running, or files are in the wrong folder | `pkill -f main.py`, check paths, restart |
| HTTP 500/422 with `WriteError` 121 | A document breaks a validator | Read `errInfo`; see [4.3](#43-database) |
| "PDF generation needs the reportlab package" | ReportLab installed outside the venv | `source .venv/bin/activate && python -m pip install reportlab`, restart |
| First answer takes ~1 min | Model loading | `OLLAMA_KEEP_ALIVE=-1` + pre-load |
| `ollama ps` shows a CPU/GPU split | Not enough VRAM | Lower `OLLAMA_CONTEXT_LENGTH` or use the Q4 model |
| Repetitive answers | Sampling | Check `QWEN_PRESENCE_PENALTY=1.5`, `QWEN_NO_THINK=1` |
| `MONGO_URI=mongodb=mongodb://…` | Typo in `config.env` | Fix the line |

---

## 13. Limitations and known issues

- **Prototype.** Results depend on how close the archived tests are to the user's conditions. This is not a safety certification.
- **Variables.** The engine models oxygen, pressure, gravity, fuel and ignition/termination outcomes. Droplet diameter, burning rate and suppressant agents (CO2, He, SF6) are not modeled yet.
- **PSI-69 (FLEX)** was uploaded as a test, but its CSV columns are not recognized yet: 0 experiments are read, the fuel is not recognized and no variables are recognized. The next step is mapping its headers.
- **Original reports.** `load_seed.js` loads records only. The "Original report" button appears only for files stored in GridFS, such as user uploads.
- **Evaluation.** Answer quality was checked by hand during development. There is no formal benchmark yet.
- **Language.** The UI is in English. The assistant answers in the language of the question.

---

## 14. Use of AI

- **In the product:** two open models run locally through Ollama. Qwen3-Embedding-0.6B handles semantic search and Qwen3.5-9B writes text. Every generated sentence is checked against database facts before it is shown, and risk alerts come from a fixed rule.
- **In development:** the team used Claude (Anthropic), an AI assistant, to help write and debug parts of the code (Flask APIs, the `motor_psi` engine, front-end JavaScript and CSS), this documentation and the presentation. The team designed the system, chose and prepared the NASA data, and tested the application.

---

## 15. Team

**Dagda-Cora**, NASA Space Apps Challenge 2026, `Monterrey, Mexico`

---

## 16. License

- **NASA data:** from NASA Physical Sciences Informatics; see each investigation's page for terms.
- **Models:** Qwen models are distributed under their own licenses; see each model card.

---

## 17. Sources

- NASA Physical Sciences Informatics: https://psi.nasa.gov
- NASA Space Apps Challenge 2026: https://www.spaceappschallenge.org/2026/
- Ollama: https://ollama.com · FAQ (server configuration): https://docs.ollama.com/faq
- `qwen3.5:9b-q8_0` on Ollama: https://ollama.com/library/qwen3.5:9b-q8_0
- `qwen3-embedding` on Ollama: https://ollama.com/library/qwen3-embedding
- Qwen3-Embedding-0.6B specs (GGUF): https://huggingface.co/Geofront/Qwen3-Embedding-0.6B-GGUF
- Qwen3.5-9B GGUF sizes: https://huggingface.co/poundingwater/astrocot
- MongoDB on Debian: https://www.mongodb.com/docs/manual/tutorial/install-mongodb-on-debian/
