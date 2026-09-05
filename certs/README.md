Put your database provider's CA certificate here as `ca.pem`, then set
`DB_SSL_CA=certs/ca.pem`.

**Aiven:** service overview page → *Connection information* → **CA
certificate** → Download. Save the file as `certs/ca.pem`.

A CA certificate is public information, not a credential — it is safe to
commit to your repository, and doing so is the simplest way to get it
onto Render. `.gitignore` deliberately does not exclude it.

Without this, the connection is still encrypted, but the client cannot
verify it is talking to your database rather than an impostor.
