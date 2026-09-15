"""slas_deploy: everything `install.sh` and `docker compose` read, rendered from code (CLAUDE.md
§3, §12; ADR-0003, ADR-0012). The base compose file, the prod override (Vault, Keycloak,
Loki, Tempo, pgBackRest, object lock, the Kata tier), the macvlan overlays for the lab and
the factory, the image lock, cosign verification, the Keycloak realm, the Vault policies,
the pgBackRest configuration, the backup runner and the restore drill.

Standard library only; tests run against fakes. Nothing here reaches a network.
"""
