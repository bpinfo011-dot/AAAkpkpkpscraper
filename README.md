# KupujemProdajem Lead Scraper

Automatski pronalazi velike prodavce na **kupujemprodajem.com** — one sa 50+ pozitivnih ocena **i** 20+ aktivnih oglasa — i pravi CSV fajl sa kontakt podacima.

## Šta dobijate

Fajl `leads.csv` sa kolonama:

| ime | link_profila | broj_ocena | broj_aktivnih_oglasa | kategorija |
|-----|-------------|------------|---------------------|-----------|
| PRODAVNICA LEPIH BROJEVA | https://kupujemprodajem.com/... | 2394 | 2819 | Mobilni tel. \| Oprema i delovi |

---

## Kako pokrenuti (korak po korak)

### 1. Napravite GitHub nalog (ako nemate)

Idite na [github.com/signup](https://github.com/signup) i napravite besplatan nalog.

### 2. Napravite novi repozitorijum

1. Kliknite zeleno dugme **"New"** na [github.com](https://github.com) (ili idite na [github.com/new](https://github.com/new))
2. Ime: `kp-scraper`
3. Stavite kvačicu na **"Add a README file"**
4. Kliknite **"Create repository"**

### 3. Dodajte fajlove u repozitorijum

Treba vam 3 fajla. Za svaki:

1. Kliknite **"Add file"** → **"Create new file"**
2. U polje za ime fajla ukucajte tačno ime (uključujući foldere)
3. Zalepite sadržaj
4. Kliknite **"Commit changes"**

Fajlovi:

| Ime fajla (ukucajte tačno ovako) | Šta je |
|---|---|
| `scraper.py` | Glavni kod |
| `.github/workflows/scrape.yml` | Automatizacija |

> **Napomena:** Kad ukucate `.github/workflows/scrape.yml`, GitHub automatski pravi foldere.

### 4. (Opciono) Postavite automatsko ponavljanje

Bez ovog koraka, svaki put kad scraper istekne (~5.5 sati), morate ga ručno ponovo pokrenuti. Sa ovim korakom radi automatski do kraja.

1. Idite na [github.com/settings/tokens](https://github.com/settings/tokens?type=beta)
2. Kliknite **"Generate new token"** → **"Fine-grained token"**
3. Ime: `kp-scraper`
4. Pristup: samo vaš `kp-scraper` repozitorijum
5. Permisije: **Contents** → Read and Write; **Actions** → Read and Write
6. Kliknite **"Generate token"** i **kopirajte token**
7. Idite u vaš `kp-scraper` repo → **Settings** → **Secrets and variables** → **Actions**
8. Kliknite **"New repository secret"**
9. Ime: `KP_PAT`
10. Vrednost: zalepite token
11. Kliknite **"Add secret"**

### 5. Pokrenite scraper

1. Idite u vaš repo → tab **"Actions"**
2. Na levoj strani kliknite **"KP Scraper"**
3. Kliknite **"Run workflow"** → **"Run workflow"**

### 6. Preuzmite rezultate

Kad se scraper završi (ili između pokretanja):

1. Idite na **Actions** tab
2. Kliknite na poslednji "workflow run"
3. Na dnu stranice, u sekciji **Artifacts**, kliknite **"kp-leads"** da preuzmete ZIP sa `leads.csv`

Takođe, `leads.csv` se automatski čuva u samom repozitorijumu.

---

## Koliko traje?

| Faza | Šta radi | Procena |
|------|----------|---------|
| Otkrivanje | Pronalazi sve kategorije i podkategorije | ~5 min |
| Skeniranje | Prolazi kroz sve oglase i beleži prodavce | 10–25 sati |
| Profili | Proverava profile najaktivnijih prodavaca | 1–3 sata |

Ukupno: **12–28 sati** (3–5 automatskih pokretanja po ~5.5h).

Cena: **$0** (GitHub Actions besplatan tier daje 2000 min/mesec).

## Često postavljana pitanja

**Mogu li da pratim napredak?**
Da. Otvorite poslednji workflow run na Actions tabu i gledajte log u realnom vremenu.

**Šta ako se scraper zaustavi usred posla?**
Sve je sačuvano u `checkpoint.json`. Sledeći run nastavlja gde je stao.

**Želim da pokrenem ispočetka.**
Obrišite fajl `checkpoint.json` iz repozitorijuma i ponovo pokrenite workflow.

**Sajt me blokira / dobijam greške.**
Scraper ima ugrađene pauze i ponovne pokušaje. Ako se greške ponavljaju, povećajte `DELAY_MIN` i `DELAY_MAX` u `scraper.py` (npr. sa 0.7/1.6 na 2.0/4.0).
