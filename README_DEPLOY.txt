QUICKINBOX — HOSTINGER VPS DEPLOYMENT

1) Copy .env.example to .env
   cp .env.example .env

2) Edit .env and put the real IMAP mailbox password.
   Never commit .env to GitHub.

3) Test locally with Docker:
   docker compose up -d --build

   Open:
   http://SERVER_IP:8501

4) For a public domain/subdomain:
   - Point an A record (for example quickinbox.example.com) to your VPS IP.
   - Install Nginx.
   - Copy nginx-quickinbox.conf.example to /etc/nginx/sites-available/quickinbox
   - Replace quickinbox.example.com with your real domain.
   - Enable the site and reload Nginx.
   - Add HTTPS with Certbot / Let's Encrypt.

Useful commands:
   docker compose ps
   docker compose logs -f
   docker compose restart
   docker compose down

IMPORTANT:
   This project requires Python/Streamlit. Hostinger Web/Cloud shared hosting
   is not the right runtime for it; use a VPS or another Python app platform.
