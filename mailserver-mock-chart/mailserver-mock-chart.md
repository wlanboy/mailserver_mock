# Helm Chart: mailserver-mock

Helm Chart für das Deployment des `mailserver-mock`-Services (SMTP/IMAP-Mock-Server für Tests) in Kubernetes.
Externer Zugriff läuft über ein Istio Ingress-Gateway: SMTP und IMAP über reine **TCP-Routen**, die
HTTP-API samt Web-UI über eine normale **HTTP-Route** auf Port 80 für `ingress.hosts`.

## Überblick

| Eigenschaft | Wert |
|---|---|
| Chart-Name | `mailserver-mock` |
| Chart-Version | 0.3.0 |
| App-Version | `latest` |
| Typ | `application` |

## Komponenten

- **Deployment** (`templates/deployment.yaml`) – 1 Replica des Containers `registry.gmk.lan/mailserver`, mit Startup-, Liveness- und Readiness-Probes per `httpGet` auf `/health`. Der Endpoint liefert nur `200`, wenn SMTP- und IMAP-Listener laufen und das Mail-Verzeichnis beschreibbar ist, und ist damit aussagekräftiger als ein reiner TCP-Check. Läuft als UID/GID 1000 mit read-only Root-Filesystem, ohne Capabilities und mit `resources`-Requests/-Limits; `/tmp` ist ein kleines `emptyDir`.
- **ConfigMap** (`templates/configmap-users.yaml`, nur bei `users.configMap.enabled: true`) – enthält die `users.json` mit Test-Usern und Fehlerszenarien, gemountet unter `/app/config/users.json`.
- **PersistentVolumeClaim** (`templates/pvc.yaml`, nur bei `pvc.enabled: true`) – wird als `/app/mails`-Volume gemountet, persistiert die als `.eml` abgelegten Test-Mails über Pod-Neustarts hinweg.
- **Service** (`templates/service.yaml`) – ClusterIP-Service mit den Ports `smtp` (1025), `imap` (1143) und `http` (8025).
- **Gateway** (`templates/gateway.yaml`) – Istio `Gateway` mit zwei `protocol: TCP`-Server-Blöcken (SMTP/IMAP-Port), `hosts: ["*"]`, da bei reinem TCP (ohne TLS-Passthrough) keine SNI-basierte Host-Unterscheidung möglich ist.
  Bei `istio.http.enabled: true` kommt ein dritter Server-Block `protocol: HTTP` auf `istio.http.port` (80) mit `hosts: ingress.hosts` hinzu.
- **VirtualService** (`templates/virtualservice.yaml`) – Istio `VirtualService` mit `tcp`-Routen (statt `http`), die anhand des Zielports zwischen SMTP und IMAP unterscheiden und beide an den Service weiterleiten.
- **VirtualService HTTP** (`templates/virtualservice-http.yaml`, nur bei `istio.http.enabled: true`) – leitet HTTP-Requests für `ingress.hosts` an den `http`-Port des Service (API + Web-UI).

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
| `service.http.port` / `service.http.targetPort` | HTTP-API/Web-UI Service- und Container-Port | `8025` |
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
| `ingress.hosts` | Hostnamen für die HTTP-Route (Web-UI/API); für SMTP/IMAP nur informativ, siehe Hinweise | `mailmock.gmk.lan` |
| `istio.gatewayName` | Name des Istio-Gateway-Objekts | `mailserver-mock-gw` |
| `istio.smtpPort` / `istio.imapPort` | Ports, die das Istio-Gateway extern für SMTP/IMAP öffnet | `1025`, `1143` |
| `istio.http.enabled` / `istio.http.port` | HTTP-Route für Web-UI/API über das Gateway | `true`, `80` |

`runAsNonRoot` steht auf Container-Ebene statt im Pod-`securityContext`: Ohne Istio-CNI läuft der
injizierte `istio-init`-Container als root, ein Pod-weites `runAsNonRoot` würde ihn blockieren.

`SMTP_HOST`/`IMAP_HOST`/`HTTP_HOST` werden im Deployment-Template fest auf `0.0.0.0` gesetzt, damit der Mock im
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

- **Kein TLS**: SMTP und IMAP sind reine TCP-Protokolle. Das Gateway terminiert kein TLS und
  routet sie ausschließlich nach Zielport, nicht nach Hostname/SNI. `ingress.hosts`
  (`mailmock.gmk.lan`) wirkt nur auf die HTTP-Route für Web-UI und API.
- **Web-UI ohne Authentifizierung**: Wie bei MailHog/Mailpit kann jeder, der `http://mailmock.gmk.lan/`
  erreicht, alle Test-Mails lesen und löschen. Bei Bedarf `istio.http.enabled: false` setzen und per
  `kubectl port-forward svc/mailserver-mock-svc 8025 -n mailmock` zugreifen.
- **Istio Ingress-Gateway-Service**: Damit SMTP/IMAP von außerhalb des Clusters erreichbar sind, muss
  der `Service` des `istio-ingressgateway` (üblicherweise `LoadBalancer`) zusätzlich die Ports 1025 und
  1143 exponieren – das ist Cluster-Konfiguration und nicht Teil dieses Charts.
- **DNS**: `mailmock.gmk.lan` muss auf die VIP/IP des Istio-Ingress-Gateways zeigen (analog zu
  `registry.gmk.lan`/`whisper.gmk.lan`).
- Für Zugriff innerhalb des Clusters (z. B. aus anderen Pods im Mesh) reicht der ClusterIP-`Service`
  direkt (`mailserver-mock-svc.mailmock.svc.cluster.local:1025`/`:1143`) – dafür ist kein Gateway nötig.

## Testen

```bash
helm template mailserver-mock ./mailserver-mock-chart --api-versions networking.istio.io/v1beta1 \
  --show-only templates/virtualservice.yaml --show-only templates/virtualservice-http.yaml
helm lint ./mailserver-mock-chart
```
