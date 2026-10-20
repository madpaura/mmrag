Company CA certificates trusted by every project's portal (mounted at /certs).
Managed with `./setup.sh trust-ca <https://server | ca-file.crt>` and listed with `./setup.sh trust-ca --list`.
To stop trusting one, delete its .crt here and run `./setup.sh deploy <proj>` (or restart the portals).
