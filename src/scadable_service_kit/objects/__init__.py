"""S3-compatible object storage (DigitalOcean Spaces).

Durable artifacts live here, never on the container filesystem: App Platform
disks are ephemeral, per replica, and capped at 4 GiB, and a full disk causes
replacement.
"""
