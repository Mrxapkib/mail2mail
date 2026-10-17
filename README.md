# mail2mail.org

IMAP mailbox migration site and online tool.

- `/` landing
- `/app` migrate
- `/docs` guide
- `/buy` order form
- `/admin` license panel (set `IMAP_ADMIN_PASSWORD`)

```bash
pip install -r requirements.txt
python app.py
```

Site: http://127.0.0.1:5055

English UI only. Brand domain: mail2mail.org

Vercel can host the pages. Long IMAP jobs need a always-on server; serverless functions do not keep a background transfer running after the request ends.
