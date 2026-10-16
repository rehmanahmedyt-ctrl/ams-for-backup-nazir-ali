# AMS — PythonAnywhere deployment

The application source is at the repository root (the original `ams.zip` is kept as an archival copy). Deployments use the signed GitHub push webhook in `deploy_hook.py`: after first-time setup, a push to the configured branch is pulled on PythonAnywhere and requests a WSGI reload.

## First-time setup for `nazirali789.pythonanywhere.com`

1. Create/open the web app on PythonAnywhere and note the **exact WSGI file path** shown in its Web tab. Open a Bash console.
2. Clone this repository into your home directory:

   ```bash
   cd ~
   git clone https://github.com/rehmanahmedyt-ctrl/ams-for-backup-nazir-ali.git
   cd ams-for-backup-nazir-ali
   ```

3. Run the guided setup with this account's domain and WSGI file path. Confirm the WSGI path against the Web tab before running:

   ```bash
   python3 setup_Deploy.py \
     --target "$HOME/ams-for-backup-nazir-ali" \
     --repo https://github.com/rehmanahmedyt-ctrl/ams-for-backup-nazir-ali.git \
     --branch main \
     --domain nazirali789.pythonanywhere.com \
     --wsgi-file /var/www/nazirali789_pythonanywhere_com_wsgi.py
   ```

   The guided setup installs dependencies, creates a private webhook secret under `instance/`, and configures the WSGI hook. It does not commit the secret or alter the application database. It will report any remaining manual Web-tab steps. If the WSGI path shown in your Web tab differs from the example above, use the path shown there.

4. In GitHub, open **Repository → Settings → Webhooks** and add a webhook:
   - Payload URL: `https://nazirali789.pythonanywhere.com/deploy`
   - Content type: `application/json`
   - Secret: the value in `instance/deploy_secret.txt` on the server (keep it private)
   - Events: **Just the push event**
   - SSL verification: enabled

   The installer can configure the GitHub webhook through the API if you supply a GitHub token using its documented `--github-token-file` option. Otherwise, configure it manually as above. Verify `https://nazirali789.pythonanywhere.com/deploy/health` and check GitHub **Recent Deliveries** for a successful signed ping.

## Ongoing deploys

The deployment branch defaults to `main` (`AMS_DEPLOY_BRANCH` in the PythonAnywhere WSGI configuration). Push or merge changes into that branch; GitHub will notify the server. The server checkout must be on `main` and have no local modifications. The hook uses `git pull --ff-only` and refuses a dirty or wrong-branch checkout. Check `https://nazirali789.pythonanywhere.com/deploy/status` for the last deploy result. The PythonAnywhere server must be able to fetch this GitHub repository; for a private repo, configure read-only Git access.

Never commit `instance/`, live database files, or webhook secrets. The included `.gitignore` excludes runtime data and secrets.
