# Put PDF Remediator online (Railway)

This gives you a private website, for example `https://pdf-remediator-production.up.railway.app`,
that you can use from any device's browser. You sign in with a password, and it runs
the same tool as the desktop app, including the automatic PDF/UA check.

**Cost:** Railway's Hobby plan is $5/month and includes $5 of usage. This app uses
about 130 MB of memory when idle, so a normal month should fit inside the included usage.
Claude API costs are separate (console.anthropic.com).

It takes about 10 minutes.

## 1. Create the Railway account

1. Go to **https://railway.com** and click **Sign in** → **GitHub**, using your GitHub
   account.
2. Choose the **Hobby** plan and add your payment method.

## 2. Deploy the app

1. Click **New Project** → **Deploy from GitHub repo**.
2. If asked, click **Configure GitHub App** and give Railway access to the
   **pdf-remediator** repository only.
3. Pick your fork of this repository. Railway finds the `Dockerfile` and starts
   building. The first build takes about 5 minutes.

## 3. Set the variables

Open the service → **Variables** → add:

| Name | Value |
|---|---|
| `APP_PASSWORD` | The password you'll sign in with. Make it long, e.g. 4+ random words |
| `SECRET_KEY` | Any long random text (keeps you signed in; never needed again) |
| `ANTHROPIC_API_KEY` | Your Claude API key, for alt text, titles and summaries (optional) |

Optional:

| Name | Value |
|---|---|
| `RETENTION_DAYS` | Auto-delete documents after this many days. Leave it unset to keep everything |
| `PDF_SERVICES_CLIENT_ID` / `PDF_SERVICES_CLIENT_SECRET` | Adobe AutoTag API, if you get quota |

## 4. Keep documents across updates

Service → **Settings** → **Volumes** → **Add volume**, and set the mount path to **`/data`**.
Without it, the document list resets every time the app updates.

## 5. Get your web address

Service → **Settings** → **Networking** → **Generate Domain**. Open that URL, sign
in with `APP_PASSWORD`, and bookmark it.

## Updating

Whenever changes are pushed to GitHub (`main`), Railway rebuilds and redeploys
automatically.

## Differences from the desktop app

- Fonts: Arial, Times and Courier are embedded as Liberation fonts, and Calibri and
  Cambria as Carlito and Caladea. These open fonts have the same character widths,
  so the layout doesn't change. Fonts with no open equivalent (e.g. Tahoma) are
  reported for Acrobat's Preflight.
- There's no "Open folder" button (the files are on the server). Use **Download PDF**.
- The learning data (your review decisions) lives on the server, in the volume.
