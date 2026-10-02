#!/usr/bin/env bash
# Deploys Evidence Atlas to the team namespace at http://<team host>/app
# (deployment/deploy-app-no-registry). Credentials are read from /config and only
# ever written into the Kubernetes Secret.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME=evidence-atlas
APP_PORT=8080

mapfile -t TEAM_CONFIGS < <(find /config -maxdepth 1 -type f -name '*.config' | sort)
(( ${#TEAM_CONFIGS[@]} == 1 )) || { echo "expected exactly one /config/*.config"; exit 1; }
set -a && source "${TEAM_CONFIGS[0]}" && set +a

if [[ -z "${KUBECONFIG:-}" ]]; then
  if [[ -f /config/kubeconfig ]]; then export KUBECONFIG=/config/kubeconfig
  else export KUBECONFIG="/config/${USERNAME}-k8s.yaml"; fi
fi
: "${COSMOS_URL:?set COSMOS_URL to the Cosmos Reason NIM base URL (see .cursor/skills/gpu/model-health)}"
NS="$USERNAME"
APP_HOST="${INGRESS_URL#http://}"; APP_HOST="${APP_HOST#https://}"; APP_HOST="${APP_HOST%%/*}"

code_files=()
for f in main.py pipeline.py index.html requirements.txt before_reingest_set02_0022.json; do
  [[ -f "$APP_DIR/$f" ]] && code_files+=(--from-file="$f=$APP_DIR/$f")
done
kubectl -n "$NS" create configmap "${APP_NAME}-code" "${code_files[@]}" --dry-run=client -o yaml | kubectl apply -f -

# Pre-built atlas and OpenStreetMap cache (build_atlas.py). Each ConfigMap is capped at 1 MiB.
for f in atlas.json.gz osm_cache.json.gz; do
  name="${APP_NAME}-data-${f%%.*}"; name="${name//_/-}"
  if [[ -f "$APP_DIR/$f" ]]; then
    kubectl -n "$NS" create configmap "$name" --from-file="$f=$APP_DIR/$f" --dry-run=client -o yaml | kubectl apply --server-side --force-conflicts -f -
  else
    kubectl -n "$NS" create configmap "$name" --dry-run=client -o yaml | kubectl apply -f -
  fi
done

# The pod cannot resolve the public team hostname, so it talks to the in-cluster backend.
kubectl -n "$NS" create secret generic "${APP_NAME}-vss-creds" \
  --from-literal=VSS_URL="http://video-backend-service:8000" \
  --from-literal=VSS_USERNAME="$USERNAME" \
  --from-literal=VSS_PASSWORD="$PASSWORD" \
  --from-literal=WANDB_API_KEY="${WANDB_API_KEY:-}" \
  --from-literal=WANDB_TEAM="${WANDB_TEAM:-}" \
  --from-literal=WANDB_PROJECT="${WANDB_PROJECT:-}" \
  --from-literal=GPU_BEARER_TOKEN="${GPU_BEARER_TOKEN:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

env_from_secret() {
  for key in "$@"; do
    cat <<EOF
        - name: ${key}
          valueFrom:
            secretKeyRef:
              name: ${APP_NAME}-vss-creds
              key: ${key}
EOF
  done
}

kubectl -n "$NS" apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${APP_NAME}
  labels:
    app: ${APP_NAME}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ${APP_NAME}
  template:
    metadata:
      labels:
        app: ${APP_NAME}
    spec:
      containers:
      - name: app
        image: python:3.12-slim
        imagePullPolicy: IfNotPresent
        ports:
        - containerPort: ${APP_PORT}
        env:
        - name: PORT
          value: "${APP_PORT}"
        - name: DATA_DIR
          value: /data
        - name: COSMOS_URL
          value: "${COSMOS_URL}"
$(env_from_secret VSS_URL VSS_USERNAME VSS_PASSWORD WANDB_API_KEY WANDB_TEAM WANDB_PROJECT GPU_BEARER_TOKEN)
        volumeMounts:
        - name: code
          mountPath: /code
        - name: atlas
          mountPath: /data/atlas.json.gz
          subPath: atlas.json.gz
        - name: osm
          mountPath: /data/osm_cache.json.gz
          subPath: osm_cache.json.gz
        workingDir: /code
        command: ["bash", "-c"]
        args:
        - |
          set -euo pipefail
          pip install --no-cache-dir -q --root-user-action=ignore -r requirements.txt
          exec python main.py
        readinessProbe:
          httpGet:
            path: /health
            port: ${APP_PORT}
          initialDelaySeconds: 15
          periodSeconds: 10
        livenessProbe:
          httpGet:
            path: /health
            port: ${APP_PORT}
          initialDelaySeconds: 40
          periodSeconds: 20
      volumes:
      - name: code
        configMap:
          name: ${APP_NAME}-code
      - name: atlas
        configMap:
          name: ${APP_NAME}-data-atlas
          optional: true
      - name: osm
        configMap:
          name: ${APP_NAME}-data-osm-cache
          optional: true
---
apiVersion: v1
kind: Service
metadata:
  name: ${APP_NAME}
  labels:
    app: ${APP_NAME}
spec:
  selector:
    app: ${APP_NAME}
  ports:
  - name: http
    port: 80
    targetPort: ${APP_PORT}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${APP_NAME}
  labels:
    app: ${APP_NAME}
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /\$2
    nginx.ingress.kubernetes.io/proxy-body-size: "32m"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "180"
spec:
  ingressClassName: nginx
  rules:
  - host: ${APP_HOST}
    http:
      paths:
      - path: /app(/|$)(.*)
        pathType: ImplementationSpecific
        backend:
          service:
            name: ${APP_NAME}
            port:
              number: 80
EOF

kubectl -n "$NS" rollout restart deploy/"$APP_NAME"
kubectl -n "$NS" rollout status deploy/"$APP_NAME" --timeout=180s
echo "http://${APP_HOST}/app"
