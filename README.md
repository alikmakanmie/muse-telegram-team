# Muse Telegram Team — Tim Karyawan AI di Grup Telegram (AgentX)

Sistem **tim karyawan AI** yang bekerja seperti perusahaan software sungguhan di dalam satu grup Telegram: ada CEO, Project Manager, Business Analyst, Desainer UI/UX, Frontend Developer, Backend Developer, DevOps, dan QA — masing-masing persona manusia dengan nama, peran, dan bot Telegram sendiri. Pemilik cukup me-mention salah satu bot di grup, dan mereka bisa **berestafet tugas antar-bot** (CEO membagi tugas → PM mengoordinasikan → Desainer membuat poster → Frontend membangun halaman → DevOps men-deploy → QA menguji), mengirim **file hasil kerja langsung ke grup**, dan memakai koneksi pemilik (Canva, Gmail, Drive, Sheets, GitHub, Vercel) untuk bekerja.

Sistem ini berjalan di atas tiga komponen:

1. **AgentX daemon** (`agentx/`) — aplikasi Node.js/TypeScript yang mendengarkan Telegram (8 bot sekaligus), me-rute pesan, mengeksekusi tool untuk karyawan, mengelola rantai bot-to-bot, dan mengirim file hasil kerja ke grup.
2. **muse-bridge** (`muse-bridge/`) — server Python kecil bergaya OpenAI-compatible yang menjembatani request model ke **sesi Muse**: setiap request menjadi "job" dalam antrean file, lalu dijawab oleh agen Muse yang sedang berjalan (itulah "otak" para karyawan).
3. **Instruksi worker** (`worker/`) — template instruksi untuk dua cron worker di Muse yang mengklaim job dari bridge dan menjawabnya sebagai persona karyawan yang tepat.

> Repo ini adalah versi publik yang sudah dibersihkan. Token bot, API key, ID Telegram, alamat server, log percakapan, dan data workspace **sengaja tidak disertakan** (lihat bagian Privasi).

---

## Arsitektur

```
                 Grup Telegram
                      │  ▲
        mention user  │  │ jawaban persona + file hasil kerja
                      ▼  │
┌────────────────────────────────────────────────────────────┐
│ AgentX daemon (Node.js, systemd, 127.0.0.1:18800)          │
│ - 8 akun bot Telegram (long-polling)                       │
│ - Routing: mention / kata kunci peran → agen yang tepat    │
│ - Eksekusi tool di workspace masing-masing karyawan        │
│ - Relay antrean bot-to-bot (estafet tim)                   │
│ - Outbox drain: file baru di .agentx/outbox/ → kirim grup  │
└───────────────┬────────────────────────────────────────────┘
                │ OpenAI-compatible API (model: dhodi)
                ▼
┌────────────────────────────────────────────────────────────┐
│ 9Router (opsional, gateway model lokal 127.0.0.1:20128)    │
└───────────────┬────────────────────────────────────────────┘
                ▼
┌────────────────────────────────────────────────────────────┐
│ muse-bridge (Python, 127.0.0.1:8765)                       │
│ /v1/chat/completions → job masuk antrean file (queue/)     │
│ Worker mengklaim job (lease), menjawab via /muse/answer    │
│ Streaming SSE ditutup segera setelah [DONE]                │
└───────────────┬────────────────────────────────────────────┘
                ▼
┌────────────────────────────────────────────────────────────┐
│ Muse (agen pribadi, sesi worker cron tiap 3 detik)       │
│ - Mengikuti worker/instruksi-worker.md                    │
│ - Menjawab sebagai persona karyawan yang diminta           │
│ - Pekerjaan berat (Canva/CLI/koneksi) dikerjakan di sini   │
└────────────────────────────────────────────────────────────┘
```

---

## Logic Penalaran (kenapa sistemnya dirancang begini)

### 1. Otak dan tangan dipisah
Daemon bersifat deterministik: mendengar, me-rute, mengeksekusi tool, mengirim file. Penalaran (memahami perintah, menyusun jawaban, memutuskan estafet) dilakukan model lewat bridge. Pemisahan ini membuat perilaku karyawan konsisten sebagai persona, sementara hal-hal yang harus pasti (file terkirim, rantai tidak loop) dijamin kode, bukan harapan pada teks model.

### 2. Jalur cepat karyawan
Job dari daemon bentuknya khas (prompt generik AgentX + roster tim + pertanyaan user). Worker diajari mengenalinya dan langsung menjawab sebagai persona yang tepat dalam target <20 detik, tanpa membaca file/memory dulu. Pertanyaan yang butuh tool dijawab dengan `tool_calls` pada POST final yang sama — daemon yang mengeksekusi, hasilnya kembali sebagai job baru.

### 3. Rantai bot-to-bot = relay antrean, bukan disiplin teks
Awalnya estafet hanya mengandalkan "@mention di balasan terakhir" — dan berulang kali putus (karyawan meneruskan ke orang yang salah, atau tidak me-mention siapa pun). Solusinya struktural, di `agentx/src/channels/router.ts`:

- Semua `@mention` pada balasan pembuka (biasanya delegasi CEO) menjadi **antrean kerja** yang pasti dijalankan satu per satu.
- Antrean juga di-*seed* dari **nama pribadi** karyawan yang disebut dalam prosa delegasi ("progres dari Nadia, Dimas, Galih, dan Kirana") — karena pemimpin tim cenderung hanya me-mention koordinatornya.
- `@mention` baru dari balasan berikutnya ditambahkan ke antrean.
- Setiap agen maksimal **satu kali per relay** (visited set) → tidak ada loop/ping-pong.
- Kedalaman maksimum 7 = satu tim penuh.

### 4. Bukti, bukan janji (outbox drain)
Model bisa berkata "sudah saya kirim" padahal belum. Karena itu pengiriman file dibuat mekanis: sebelum tugas berjalan, router memotret isi `<workspace>/.agentx/outbox/`; sesudahnya, file baru/berubah **dikirim ke grup oleh daemon** (maks 5 file, 49 MB) lalu dihapus. Adapter Telegram ditambah unggahan multipart (`apiUpload`) karena API JSON biasa hanya menerima URL publik. Instruksi worker melengkapinya dengan aturan: klaim "sudah jadi" wajib disertai deliverable yang terkirim di putaran yang sama; status yang belum tuntas dilaporkan apa adanya.

### 5. Dua perbaikan latensi/keandalan yang penting
- **SSE harus ditutup setelah `[DONE]`.** Bridge semula mengirim `Connection: keep-alive` dan tidak menutup koneksi; klien daemon membaca sampai koneksi tertutup sehingga jawaban yang sebenarnya selesai dalam ~3 detik menggantung sampai timeout (terukur 143 detik → **12–13 detik** setelah diperbaiki di `bridge.py`).
- **Permission mode harus mengikuti config.** Tool karyawan sempat menggantung selamanya karena prompt izin interaktif (Yes/No) muncul di daemon tanpa TTY. Di `agentx/src/agents/runtime.ts`, mode izin kini disetel dari `permissionMode` agen sebelum eksekusi.

### 6. Koneksi pemilik dengan pagar
Worker boleh memakai koneksi pemilik (Gmail/Drive/Sheets via CLI, GitHub, Vercel, Canva) di sesinya sendiri saat tugas membutuhkannya, dengan pagar: membaca bebas; **mengirim/mengubah/menghapus/deploy/merge hanya atas perintah eksplisit pemilik**; tool pembelian dilarang keras; untuk layanan yang belum terhubung, karyawan wajib jujur, bukan berpura-pura bisa.

### 7. Persona yang konsisten
Karyawan tampil sebagai manusia (nama + peran), tidak menyebut diri AI/model. Roster, gaya bicara, dan aturan tim (termasuk "sebut rekan dengan @username dalam konteks tugas") hidup di instruksi worker dan system prompt tiap agen di config — bukan di kode.

---

## Cara Menjalankan di Muse

### Prasyarat
- Sebuah mesin Linux (VM) yang selalu menyala, dengan **Node.js 20+** (kami memakai v24) dan **Python 3**.
- **Muse** berjalan di mesin yang sama (agen inilah yang menjadi otak worker).
- **Bot Telegram** untuk tiap karyawan, dibuat via [@BotFather](https://t.me/BotFather) (8 bot untuk tim lengkap). Catat tokennya.
- Satu **grup Telegram** berisi pemilik + semua bot. Mode grup bawaan: bot hanya merespons saat di-mention (kalau mau bot membaca semua pesan, atur `/setprivacy` → Disable di BotFather untuk tiap bot).
- Opsional: **9Router** sebagai gateway model lokal. Tanpa 9Router, arahkan `OPENAI_BASE_URL` langsung ke bridge (`http://127.0.0.1:8765/v1`).

### Langkah
1. **Clone repo ini**, masuk ke folder `agentx/`, lalu build daemon:
   ```bash
   npm install
   npx tsup        # error DTS adalah bawaan upstream, abaikan — bundle JS tetap jadi
   ```
2. **Siapkan config karyawan**: salin `agentx/agentx.example.json` menjadi `agentx/agentx.json`. Sesuaikan nama/persona/peran bila perlu. Salin `agentx/.env.example` menjadi `agentx/.env`, isi token bot dari BotFather + API key gateway modelmu, lalu `chmod 600 .env agentx.json`.
3. **Buat kunci bridge** (dua file teks berisi string acak panjang, permission 600), misalnya di `muse-bridge/secrets/`: `bridge-user.key` (untuk klien/API) dan `bridge-worker.key` (untuk worker). Bridge membaca kunci dari file — tidak ada kunci yang tertanam di kode.
4. **Jalankan bridge**: sesuaikan path di `muse-bridge/systemd/muse-bridge.service` (ganti `/home/USER` dengan home-mu), salin ke `/etc/systemd/system/`, lalu `systemctl daemon-reload && systemctl enable --now muse-bridge`. Cek: `curl http://127.0.0.1:8765/health`. Lease job bawaan 180 detik; untuk alur berat seperti Canva naikkan env `BRIDGE_LEASE_SECS=300`.
5. **(Opsional) 9Router**: jalankan sebagai service (`muse-bridge/systemd/9router.service`); daftarkan provider OpenAI-compatible yang menunjuk ke bridge (`http://127.0.0.1:8765/v1`, key = isi `bridge-user.key`) dan buat combo model, mis. `dhodi` → provider itu. Set env `NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt` bila trafikmu melewati proxy TLS-intercepting. Di `agentx.json`, nama provider untuk jalur OpenAI-compatible harus salah satu yang dikenal AgentX (`openai`), dan `baseUrl` efektif dibaca dari env `OPENAI_BASE_URL` di `.env` daemon — bukan dari field config.
6. **Jalankan daemon**: sesuaikan `agentx/systemd/agentx-daemon.service`, salin ke `/etc/systemd/system/`, `systemctl enable --now agentx-daemon`. Cek: `curl http://127.0.0.1:18800/health` → harus melaporkan jumlah agen dan akun Telegram yang mulai.
7. **Hidupkan otak di Muse**: di aplikasi Muse, buat **dua cron worker** (interval ±3 detik, bergantian) memakai isi `worker/instruksi-worker.md` sebagai instruksinya — sesuaikan path bridge/secrets dengan mesinmu. Worker inilah yang menjawab setiap job sebagai persona karyawan. (Skrip pollingnya ada di `muse-bridge/worker-loop.py`.)
8. **Uji di grup**: mention bot CEO, mis. `@BOT_ceo halo, perkenalkan timnya`. Lalu uji estafet: minta CEO menanyakan progres ke beberapa karyawan sekaligus — mereka harus menjawab berurutan. Terakhir uji file: minta desainer membuat poster — PNG-nya harus tiba di grup sebagai file, bukan hanya disebut namanya.

### Pemulihan setelah VM diganti (opsional)
`muse-bridge/systemd/recover-after-vm-replacement.sh` adalah contoh skrip pemulihan satu perintah untuk lingkungan kami (VM yang bisa ter-*replace* dan me-wipe unit systemd): memasang ulang service dari salinan canonical di workspace. Sesuaikan dengan lingkunganmu atau abaikan bila tidak relevan.

---

## Struktur Repo

```
agentx-telegram-team/
├── README.md                  ← dokumen ini
├── agentx/
│   ├── src/                   ← kode daemon (upstream AgentX + patch kami:
│   │                            router relay antrean + outbox drain,
│   │                            unggahan multipart Telegram, fix permission)
│   ├── package.json, tsconfig.json, tsup.config.ts, ...
│   ├── agentx.example.json    ← contoh config 8 karyawan (token = placeholder)
│   ├── .env.example           ← contoh variabel lingkungan
│   ├── LICENSE                ← lisensi MIT upstream
│   └── systemd/agentx-daemon.service
├── muse-bridge/
│   ├── bridge.py              ← server antrean + API OpenAI-compatible
│   ├── fast-answer.py         ← jalur cepat opsional (model API eksternal)
│   ├── worker-loop.py         ← skrip polling worker
│   └── systemd/               ← muse-bridge.service, 9router.service,
│                                recover-after-vm-replacement.sh
└── worker/
    └── instruksi-worker.md    ← template instruksi worker Muse
                                 (persona, jalur cepat, Canva, koneksi, estafet)
```

---

## Privasi — yang sengaja TIDAK disertakan

Repo ini diterbitkan tanpa data privat apa pun:

- Token bot Telegram asli dan semua API key (hanya ada placeholder `${...}`).
- File `.env`, `agentx.json` asli, folder `secrets/`, dan `keys.json`.
- ID Telegram pemilik & grup, alamat IP server, dan domain pribadi (diganti placeholder).
- Log percakapan grup, riwayat tugas, memori tim, dan isi workspace karyawan.
- Nama bot asli diganti pola generik (`@BOT_ceo`, dst.) — sesuaikan dengan bot-mu sendiri.

Sebelum memakai, telusuri sekali config hasil salinanmu dan pastikan tidak ada nilai asli yang tertinggal.

## Lisensi & Atribusi

- Kode di `agentx/` berasal dari proyek **AgentX / agentix-cli** — MIT License, Copyright (c) 2026 Anis Marrouchi (lihat `agentx/LICENSE`). Patch pada router, adapter Telegram, dan runtime adalah kontribusi pemilik repo ini.
- `muse-bridge/` dan `worker/` adalah karya pemilik repo ini, dirilis dengan lisensi yang sama (MIT).
