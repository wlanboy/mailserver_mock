# Helm Chart: mailserver-mock

Helm Chart für das Deployment des `mailserver-mock`-Services (SMTP/IMAP-Mock-Server für Tests) in Kubernetes.
Externer Zugriff läuft über ein Istio Ingress-Gateway mit reinen **TCP-Routen** (kein HTTP/HTTPS), da SMTP
und IMAP keine HTTP-Protokolle sind.

## Überblick

| Eigenschaft | Wert |
|---|---|
| Chart-Name | `mailserver-mock` |
| Chart-Version | 0.2.0 |
| App-Version | `latest` |
| Typ | `application` |

## Komponenten

- **Deployment** (`templates/deployment.yaml`) – 1 Replica des Containers `registry.gmk.lan/mailserver`, mit TCP-basierten Startup-, Liveness- und Readiness-Probes (SMTP- bzw. IMAP-Port), da kein HTTP-Endpoint existiert. Läuft als UID/GID 1000 mit read-only Root-Filesystem, ohne Capabilities und mit `resources`-Requests/-Limits; `/tmp` ist ein kleines `emptyDir`.
- **ConfigMap** (`templates/configmap-users.yaml`, nur bei `users.configMap.enabled: true`) – enthält die `users.json` mit Test-Usern und Fehlerszenarien, gemountet unter `/app/config/users.json`.
- **PersistentVolumeClaim** (`templates/pvc.yaml`, nur bei `pvc.enabled: true`) – wird als `/app/mails`-Volume gemountet, persistiert die als `.eml` abgelegten Test-Mails über Pod-Neustarts hinweg.
- **Service** (`templates/service.yaml`) – ClusterIP-Service mit den Ports `smtp` (1025) und `imap` (1143).
- **Gateway** (`templates/gateway.yaml`) – Istio `Gateway` mit zwei `protocol: TCP`-Server-Blöcken (SMTP/IMAP-Port), `hosts: ["*"]`, da bei reinem TCP (ohne TLS-Passthrough) keine SNI-basierte Host-Unterscheidung möglich ist.
- **VirtualService** (`templates/virtualservice.yaml`) – Istio `VirtualService` mit `tcp`-Routen (statt `http`), die anhand des Zielports zwischen SMTP und IMAP unterscheiden und beide an den Service weiterleiten.

Beide Istio-Templates prüfen zusätzlich über `.Capabilities.APIVersions.Has`, ob die Istio-CRDs im
Zielcluster vorhanden sind – ein `helm install`/`helm template` schlägt also nicht fehl, falls Istio
(noch) nicht installiert ist.

## Konfiguration (`values.yaml`)

| Key | Beschreibung | Default |
|---|---|---|
| `image.repository` / `image.tag` / `image.pullPolicy` | Container-Image | `registry.gmk.lan/mailserver`, `latest`, `Always` |
| `replicaCount` | Anzahl Replicas | `1` |
| `deploymentName` | Name für Deployment und Ingress-Ressourcen | `mailserver-mock` |
| `service.name` | Name des Service-Objekts | `mailserver-mock-svc` |
| `service.type` | Service-Typ | `ClusterIP` |
| `service.smtp.port` / `service.smtp.targetPort` | SMTP Service- und Container-Port | `1025` |
| `service.imap.port` / `service.imap.targetPort` | IMAP Service- und Container-Port | `1143` |
| `pvc.enabled` | PVC für `/app/mails` anlegen | `true` |
| `pvc.name` / `pvc.accessModes` / `pvc.storageClassName` / `pvc.size` | PVC-Einstellungen | `mailserver-mock-mails-pvc`, `[ReadWriteOnce]`, `""`, `256Mi` |
| `volume.mountPath` | Mountpfad des Mail-Volumes im Container | `/app/mails` |
| `env.mailDir` | `MAIL_DIR` im Container | `/app/mails` |
| `env.usersFile` | `USERS_FILE` im Container, nur bei `users.configMap.enabled: false` (Datei aus dem Image) | `/app/users.json` |
| `resources` | CPU/Memory-Requests und -Limits | `50m`/`64Mi` Request, `250m`/`128Mi` Limit |
| `podSecurityContext` | Pod-`securityContext` (`fsGroup` für Schreibrechte auf dem PVC, Seccomp) | `fsGroup: 1000`, `RuntimeDefault` |
| `securityContext` | Container-`securityContext` | `runAsNonRoot`, UID/GID `1000`, `readOnlyRootFilesystem`, `drop: [ALL]` |
| `users.configMap.enabled` / `users.configMap.name` | `users.json` als ConfigMap statt aus dem Image | `true`, `mailserver-mock-users` |
| `users.mountPath` | Mount-Verzeichnis der ConfigMap, `USERS_FILE` wird auf `<mountPath>/users.json` gesetzt | `/app/config` |
| `users.config` | User-Liste als YAML, wird zu JSON gerendert | Inhalt von `users.json` im Repo-Root |
| `users.json` | Roh-JSON, hat Vorrang vor `users.config` (für `--set-file`) | `""` |
| `ingress.hosts` | Hostname, unter dem der Service dokumentiert erreichbar ist (nur informativ, siehe Hinweise) | `mailmock.gmk.lan` |
| `istio.gatewayName` | Name des Istio-Gateway-Objekts | `mailserver-mock-gw` |
| `istio.smtpPort` / `istio.imapPort` | Ports, die das Istio-Gateway extern für SMTP/IMAP öffnet | `1025`, `1143` |

`runAsNonRoot` steht auf Container-Ebene statt im Pod-`securityContext`: Ohne Istio-CNI läuft der
injizierte `istio-init`-Container als root, ein Pod-weites `runAsNonRoot` würde ihn blockieren.

`SMTP_HOST`/`IMAP_HOST` werden im Deployment-Template fest auf `0.0.0.0` gesetzt, damit der Mock im
Container von außerhalb von `localhost` erreichbar ist (Default im Code ist `127.0.0.1`).

## Installation

```bash
kubectl create namespace mailmock
kubectl label namespace mailmock istio-injection=enabled --overwrite
helm install mailserver-mock ./mailserver-mock-chart -n mailmock --create-namespace

kubectl get gateway,virtualservice -n mailmock
```

Das `istio-injection`-Label wird benötigt, damit der Pod einen Sidecar erhält (das Deployment-Template
setzt zwar `sidecar.istio.io/inject: "true"`, das greift aber nur bei aktivierter Namespace-Injection).

## Upgrade

```bash
helm upgrade mailserver-mock ./mailserver-mock-chart -n mailmock --create-namespace
helm upgrade mailserver-mock ./mailserver-mock-chart -n mailmock --set image.tag=1.2.3
```

## Test-User / Fehlerszenarien anpassen

`users.json` wird bei jedem Login neu eingelesen, und die ConfigMap ist ohne `subPath` gemountet.
Geänderte User landen daher ohne Image-Rebuild und ohne Pod-Restart im Container (Kubelet-Sync,
meist unter einer Minute):

```bash
# Repo-users.json direkt übernehmen
helm upgrade mailserver-mock ./mailserver-mock-chart -n mailmock --set-file users.json=users.json

# oder eigene Values-Datei mit users.config.users: [...]
helm upgrade mailserver-mock ./mailserver-mock-chart -n mailmock -f my-users.yaml
```

Ein `-f` mit `users.config.users` ersetzt die Liste komplett (Helm merged Listen nicht).

## Deinstallation

```bash
helm uninstall mailserver-mock -n mailmock
```

> Der PVC (`pvc.name`) wird von Helm mit deinstalliert, da er Teil der gerenderten Ressourcen des
> Release ist. Test-Mails in `/app/mails` gehen dabei verloren – für einen reinen Mock in der Regel
> unkritisch.

## Hinweise

- **Kein HTTP/TLS**: SMTP und IMAP sind reine TCP-Protokolle. Das Gateway terminiert kein TLS und
  routet ausschließlich nach Zielport, nicht nach Hostname/SNI – `ingress.hosts` (`mailmock.gmk.lan`)
  ist daher rein dokumentarisch (z. B. für den DNS-Eintrag) und fließt nicht in Gateway/VirtualService
  ein.
- **Istio Ingress-Gateway-Service**: Damit SMTP/IMAP von außerhalb des Clusters erreichbar sind, muss
  der `Service` des `istio-ingressgateway` (üblicherweise `LoadBalancer`) zusätzlich die Ports 1025 und
  1143 exponieren – das ist Cluster-Konfiguration und nicht Teil dieses Charts.
- **DNS**: `mailmock.gmk.lan` muss auf die VIP/IP des Istio-Ingress-Gateways zeigen (analog zu
  `registry.gmk.lan`/`whisper.gmk.lan`).
- Für Zugriff innerhalb des Clusters (z. B. aus anderen Pods im Mesh) reicht der ClusterIP-`Service`
  direkt (`mailserver-mock-svc.mailmock.svc.cluster.local:1025`/`:1143`) – dafür ist kein Gateway nötig.

## Testen

```bash
helm template mailserver-mock ./mailserver-mock-chart --show-only templates/virtualservice.yaml
helm lint ./mailserver-mock-chart
```
