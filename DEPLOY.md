# Deploying studio-extractor

The app is a single Docker image (see `Dockerfile`). It listens on `$PORT`
(default 8080) and serves `/health`.

## Northflank (free Sandbox, no card)
1. Sign up at https://app.northflank.com/signup (GitHub login works).
2. Link GitHub: Account settings → Git → GitHub → grant access to `azzaz796-maker/studio-extractor`.
3. Create project (region closest to users, e.g. Europe) → Create service → **Combined service**.
4. Repository `azzaz796-maker/studio-extractor`, branch `main`, build type **Dockerfile**, path `/Dockerfile`, context `/`.
5. Networking: port **8080**, protocol HTTP, **Publicly expose**.
6. Resources: the largest plan that fits the free Sandbox allowance (aim for 0.5 vCPU / 1 GB); ephemeral storage as large as allowed (uploads up to 800 MB + enhance output).
7. Health check: HTTP `GET /health` on 8080.
8. Create → wait for build → open the `*.code.run` URL.

## Render (still compatible)
Render sets `PORT=10000`; no changes needed.
