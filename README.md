# PVH TOPUP — Single-file HTML + Python Server

## អ្វីដែលបានកែ

- `index.html` — frontend ទាំងមូល (JS + CSS ដើម) ត្រូវបានបញ្ចូលទៅជា **ឯកសារតែមួយ** (embed ជា base64, decode ហើយ load ជា module នៅ runtime)
- `server.py` — Python (Flask) server ថ្មី ជំនួស Netlify Functions ទាំងអស់ ដោយប្រើ **`db.json`** ជំនួស Supabase (បង្កើតដោយស្វ័យប្រវត្តិពី `db_default.json` ពេលដំណើរការលើកដំបូង)
- `admin/index.html` — admin panel ដដែល (បានជា static HTML+JS រួចស្រាប់ គ្មានអី ត្រូវ merge បន្ថែម)

## ចំណាំសំខាន់ (កម្រិត)

⚠️ ជា bundle ដើម មាន lazy-loaded route ចំនួន **2** (`DgwKVVB1.js`, `B_9XU-01.js`) ដែល**មិនមាននៅក្នុង zip ដែលបានផ្ញើមក**។ ផ្នែកសំខាន់ៗ (ទំព័រដើម, បញ្ជីហ្គេម, ការទូទាត់/QR) ដំណើរការធម្មតា ប៉ុន្តែ route ជាក់លាក់ 2 នេះនឹង error ក្នុង console។ បើអ្នកមានឯកសារ 2 នេះ ផ្ញើមកខ្ញុំអាចបញ្ចូលបន្ថែមបាន។

## របៀបដំណើរការ

```bash
pip install -r requirements.txt --break-system-packages
cp .env.example .env   # រួចដាក់តម្លៃពិត (CAMRAPID_API_KEY, TELEGRAM_BOT_TOKEN, ADMIN_PANEL_TOKEN...)
python server.py
```

- គេហទំព័រ: `http://localhost:5000/`
- Admin panel: `http://localhost:5000/admin` (លេខសម្ងាត់ = `ADMIN_PANEL_TOKEN`)

## Endpoints (ដូចគ្នានឹង netlify functions ដើមទាំងអស់)

`/api/create-payment`, `/api/check-payment`, `/api/expire-payment`, `/api/check-topup-status`,
`/api/get-home-data`, `/api/get-topup-data`, `/api/check-user`, `/api/get-stats`, `/api/get-site-settings`,
`/api/admin-settings`, `/api/admin-games`, `/api/admin-products`, `/api/admin-banners`, `/api/admin-transactions`

## ទិន្នន័យ

ទាំងអស់រក្សាទុកក្នុង `db.json` (auto-created) — មាន `games`, `products`, `banners`, `transactions`, `site_settings`។
បន្ថែម games/products/banners ដំបូងតាមរយៈ admin panel (`/admin`) ព្រោះ `db.json` ចាប់ផ្តើមទទេ។

## Deploy

- Render/VPS/Termux ធម្មតា៖ `python server.py` (កំណត់ `PORT` env បើត្រូវការ)
- ត្រូវការ HTTPS domain ពិត ដើម្បីឲ្យ CamRapidPay webhook/redirect ដំណើរការត្រឹមត្រូវ (ដូច bot ដទៃទៀតរបស់អ្នក)

## Deploy លើ Render (Web Service)

1. Push folder `pvh_server/` នេះទាំងមូលទៅ GitHub repo
2. លើ Render dashboard → **New → Web Service** → ភ្ជាប់ repo
3. Render នឹងអាន `render.yaml` ដោយស្វ័យប្រវត្តិ (Build: `pip install -r requirements.txt`, Start: `gunicorn server:app --bind 0.0.0.0:$PORT`)
4. ចូល **Environment** tab → បំពេញតម្លៃពិតសម្រាប់ (`sync: false` ក្នុង render.yaml មានន័យថា Render នឹងសួរអ្នកបំពេញផ្ទាល់)៖
   - `CAMRAPID_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `ADMIN_CHAT_ID`
   - `TOPUP_PROVIDER_TOKEN` (ស្រេចចិត្ត)
   - `ADMIN_PANEL_TOKEN` និង `SIGNING_SECRET` កំណត់ស្រេចហើយក្នុង `render.yaml` (មិនចាំបាច់ប្តូរ)
5. **Disk**: `render.yaml` បានកំណត់ persistent disk (`/opt/render/project/src/data`) ដើម្បីរក្សា `db.json` កុំឲ្យបាត់ពេល redeploy/restart។ បើ Render មិន support disk លើ free plan របស់អ្នក អាចលុប `disk:` block ចេញ ប៉ុន្តែ **ទិន្នន័យនឹងបាត់រាល់ដង deploy ថ្មី** (ត្រូវបញ្ចូល games/products/banners ក្នុង admin panel ម្តងទៀត)
6. ចុច **Create Web Service** → រង់ចាំ build ចប់ → បើក URL ដែល Render ផ្តល់ (ឧ. `https://pvh-topup.onrender.com`)
7. ចូល `/admin` វាយ password: `1jcVF54vpA2591jnBMQNsvlmIL6AgkwAdmVPr1gr7H0`

**Free plan note**: Render free web services "sleep" ពេលមិនមានគេចូលរយៈពេលមួយ — request ដំបូងក្រោយ sleep នឹងយឺតបន្តិច (cold start ~30s)។ បើមិនចង់ឲ្យ sleep ត្រូវប្រើ paid plan។
