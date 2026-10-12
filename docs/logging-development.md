# Logging development in monitoring-console-plugin

Logging was imported from `openshift/logging-view-plugin` revision
`eaaf8c5311b8675a38e9a3b913e2b51174848620`, including the COO-2519 row-resize fix. The source Apache-2.0
license is retained in `web/src/features/logging/LICENSE`; feature reviewers are
in that directory's `OWNERS`. Components were renamed to destination conventions,
imports use `@/`, and the 14 source unit suites run in Vitest.

Logging is **opt-in**. Neither `ALL_FEATURES`, `MCP_DEVSPACE_FEATURES`, nor either
frontend default feature set enables it. The Go manifest handler adds all ten
source extensions only for `-features=logging`: Observe > Logs, pod Aggregated
Logs, the supported developer tab, user/platform rule sources in both alerting
contexts, and their charts. CMO continues to supply the ordinary monitoring UI.

## Set up log collection

The development helper below expects logs to already be in Loki. On a cluster
with a working LokiStack, this script installs the Red Hat OpenShift Logging
Operator if needed, grants the collector its application/infrastructure roles,
and creates the `ClusterLogForwarder` that sends those logs to Loki:

```sh
LOKI_NAMESPACE=openshift-logging LOKI_STACK=logging-loki \
  LOGGING_CHANNEL=stable-6.5 ./hack/setup-logging-forwarder.sh
```

It checks that the Logging Operator channel matches the installed Loki Operator
channel. The default Loki gateway CA ConfigMap is `openshift-service-ca.crt`; set
`LOKI_CA_CONFIGMAP` if the existing stack uses another CA ConfigMap. An existing
matching `ClusterLogForwarder/instance` is kept; a forwarder targeting another
stack is left untouched and causes the script to stop. This config collects
application and infrastructure logs, not audit logs.

The script requires cluster-admin access and an existing `openshift-logging`
namespace and LokiStack. Loki's object storage setup is intentionally left to
cluster-specific provisioning because it needs working storage credentials and
configuration. For the operator's manual setup reference, see [Red Hat Logging
6.5 installation](https://docs.redhat.com/en/documentation/red_hat_openshift_logging/6.5/html/installing_logging/installing-the-red-hat-openshift-logging-operator).

## The COO logging bridge

A temporary COO bridge lets the COO-managed MCP serve merged logging before
the final UIPlugin consolidation. It requires a COO build containing the
bridge; a stock COO
ignores the annotations and the setup fails fast instead of reporting
success. Two experimental annotations control it, and only the exact value
`"true"` enables each control:

- `observability.openshift.io/ui-plugin-experimental-logging: "true"` on the
  Monitoring UIPlugin opts the CR into interpreting its `spec.logging`.
- `observability.openshift.io/ui-plugin-console-disabled: "true"` on the
  Logging UIPlugin keeps the standalone resources reconciled but removes
  `logging-view-plugin` from the Console registration. This is Console
  disabling only: the standalone deployment, its UIPlugin, Loki data and
  roles are preserved, and log collection and user permissions are
  independent of it.

The opted-in Monitoring CR looks like this (combine with an existing
`spec.monitoring` for combined testing; keep logging settings on this CR):

```yaml
apiVersion: observability.openshift.io/v1alpha1
kind: UIPlugin
metadata:
  name: monitoring
  annotations:
    observability.openshift.io/ui-plugin-experimental-logging: "true"
spec:
  type: Monitoring
  logging:
    lokiStack:
      name: logging-loki
      namespace: openshift-logging
    logsLimit: 100
    timeout: 30s
    schema: select
    showTimezoneSelector: true
```

COO then enables the `logging` runtime feature, appends the authenticated
`loki` proxy targeting `<LokiStack name>-gateway-http` on port 8080 to the
MCP ConsolePlugin (preserving `backend` and any ACM/Perses proxies), and
generates an MCP-owned ConfigMap with the settings nested beneath `logging`
mounted at `/etc/plugin/config/config.yaml`. The LokiStack reference is not
part of the frontend configuration.

To test with custom images, build and push a COO image containing the bridge
and an MCP image containing merged logging, then select the custom MCP with
COO's existing `--images` option using the monitoring slot for the cluster
version (`ui-monitoring-pf5` for OCP 4.15–4.18, `ui-monitoring-pf6` for
4.19–4.21, `ui-monitoring` for 4.22+), e.g.
`--images=ui-monitoring=quay.io/<org>/monitoring-console-plugin:<tag>`.
These are image-selection slots, not a compatibility claim: validate the
actual Console SDK/PatternFly compatibility of the merged MCP build and
prefer a known-compatible cluster. For OLM-managed installations prefer a
custom development bundle/catalog; direct edits of an OLM-managed operator
Deployment may be reverted. Alternatively pause the installed COO and run
one custom COO instance separately with the required permissions — never
two COO instances managing the same resources. With a running bridged COO you can
also test merged logging directly through the real Console without DevSpace:
apply the CRs above, use the Console, then roll back by removing the
experimental-logging annotation first and the console-disabled annotation
after the extensions are gone.

### Published temporary test bundle

The temporary OU-1595 images are available for testing before final COO integration:

- Bundle: `quay.io/rh-ee-dchromik/observability-operator-bundle:0.0.0-ou1595`
- COO: `quay.io/rh-ee-dchromik/observability-operator:0.0.0-ou1595`
- MCP: `quay.io/rh-ee-dchromik/monitoring-console-plugin:ou1595-bridge`

On a disposable OpenShift 4.22+ / 5.x cluster without an existing COO installation:

```sh
oc create namespace openshift-cluster-observability-operator \
  --dry-run=client -o yaml | oc apply -f -
operator-sdk run bundle \
  quay.io/rh-ee-dchromik/observability-operator-bundle:0.0.0-ou1595 \
  --namespace openshift-cluster-observability-operator \
  --install-mode AllNamespaces --security-context-config restricted
```

Loki, collection and user permissions must be provisioned separately. Disable the
standalone logging registration before applying the Monitoring CR above. The
operator repository's `docs/developer.md` documents the opt-in
`deploy/olm-logging-dev` overlay and bundle generation. Test-image references and
generated artifacts must stay separate from the normal release defaults when
preparing the PR. Published images do not pick up local fixes; use DevSpace below,
or rebuild MCP with a new tag to validate the latest frontend changes by image.

## Run against a development cluster

Prerequisites: Node 22+, installed frontend/Go dependencies, `kubectl`, `oc`,
Python 3, DevSpace, a logged-in OpenShift Console, and a reachable current
kubeconfig context. Use a test cluster with a LokiStack gateway on service port
8080 and actual collected logs. Your Console user's existing Loki/namespace
permissions determine access; the DevSpace workflow grants no roles and installs
no log collector or LokiStack. Console must support the extension points in SDK
4.22; developer views depend on Console's developer perspective support.

```sh
make install-frontend
# Terminal 1; keep the MCP identity, including its translation namespace.
make start-mcp-frontend

# Terminal 2; defaults shown explicitly. Select mcp-logging when prompted.
export LOKI_NAMESPACE=openshift-logging
export LOKI_STACK=logging-loki
devspace dev

# Cleanup: select mcp-logging again (or use the deterministic form below).
devspace purge --var PLUGIN=mcp-logging
```

The deterministic startup is `devspace dev --var PLUGIN=mcp-logging`. It requires
a running COO with the logging bridge; fallback startup is no longer supported.
Setup checks the Loki gateway and saves the original COO replicas, affected CR
settings/annotations and Console registrations in `.devspace/logging/state.json`
before mutation. It disables standalone logging registration, applies the
Monitoring CR, verifies the generated MCP resources and authenticated `loki`
proxy, then pauses COO for DevSpace's deployment takeover. The Makefile is synced
as well, so the new target works with the existing development image.

On purge, DevSpace first restores the deployment. The helper restores the exact
original `spec.logging` and experimental annotation, resumes COO at its original
replica count, and waits for MCP's original logging feature/proxy state (which may
already have been enabled). Session-created Monitoring CRs are deleted while COO
is running. Only then does it restore standalone logging registration, avoiding
duplicate logging extensions during rollback. Unrelated CR fields and Console
registrations are preserved.

Keep the state file until cleanup succeeds. Cleanup checks the saved kubeconfig
context and can be retried after partial failure. Recovery of older fallback
sessions remains supported so their resources are not stranded; new sessions
always use COO provisioning.

If setup fails partway through, run the same purge command. Do not switch contexts
or remove `.devspace/logging` before cleanup. As with ordinary DevSpace, stop the
session before purging so DevSpace restores the deployment it replaced.

By default, the COO-generated development config is:

```yaml
logging:
  logsLimit: 100
  timeout: 30s
  schema: select
  showTimezoneSelector: true
```

The CR drives the ConfigMap. Setup uses `LOKI_NAMESPACE`/`LOKI_STACK` and the
optional `LOGGING_LOGS_LIMIT`, `LOGGING_TIMEOUT`, `LOGGING_SCHEMA` and
`LOGGING_SHOW_TIMEZONE_SELECTOR` variables. Without a local override, the launcher
reads the COO-mounted `/etc/plugin/config/config.yaml`. Default-page streaming is
disabled unless enabled through a local override.

To supply different settings, set `LOGGING_CONFIG=/absolute/path/config.yaml`
before startup. This is the **whole MCP configuration**, with logging nested as
above; top-level `timeout` controls the shared server/ACM timeout independently.
It overrides the operator-generated config only for the development runtime,
and remains the way to use settings COO does not expose yet, such as
`isStreamingEnabledInDefaultPage`, plus quick local iteration. Changing the
CR while COO is paused does not regenerate its ConfigMap: change CR-driven
settings between sessions, or use the local override during a session.
`logging.timeout` is a Go duration in YAML and seconds in the frontend JSON
response. Source defaults when absent remain 100 lines, ViaQ, and no
default-page streaming. Existing browser preference keys are deliberately
retained. Other source options (`useTenantInHeader`,
`alertingRuleTenantLabelKey`, `alertingRuleNamespaceLabelKey`, and
`showTimezoneSelector`) remain supported.

Check the Console requests: `/api/plugins/monitoring-console-plugin/features`
must contain `logging: true`, and all Loki HTTP and WebSocket requests must use
`/api/proxy/plugin/monitoring-console-plugin/loki`. The alias forwards the logged-in
user's token through Console (`authorization: UserToken`), preserving Loki tenant
authorization. The existing MCP `backend` alias is preserved.

## Enabled, disabled and combined runs

Purge between each run to restore the initial resources and operator replicas.
Keep the MCP frontend watcher running.

```sh
# Logging only; no ACM arguments or dependencies.
LOGGING_DEV_FEATURES=logging devspace dev --var PLUGIN=mcp-logging
# ...exercise logging, then:
devspace purge --var PLUGIN=mcp-logging

# Disabled logging, existing optional feature enabled; legacy logging still
# deregistered during this run so it cannot hide a failed feature gate.
LOGGING_DEV_FEATURES=cluster-health-analyzer devspace dev --var PLUGIN=mcp-logging
# ...verify no logging extensions, navigation or Loki requests, then:
devspace purge --var PLUGIN=mcp-logging

# Combined; use a cluster already providing the optional feature's services.
LOGGING_DEV_FEATURES=logging,cluster-health-analyzer devspace dev --var PLUGIN=mcp-logging
# ...exercise both features, then:
devspace purge --var PLUGIN=mcp-logging
```

For ACM/Perses combinations, explicitly list the required existing MCP features
in `LOGGING_DEV_FEATURES` and use a cluster already configured for those services.
ACM proxy arguments are supplied only when `acm-alerting` is selected. This mode
never provisions ACM or Perses implicitly.

## Tests

`make test-frontend` includes imported logging unit tests plus nested config
coverage. `make test-backend` includes manifest gating, config serialization and
logging-only startup without ACM. The workflow restoration checks run without a
cluster:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s hack -p 'test_logging_devspace.py'
```

All five source mocked Cypress integration suites (alerts, pod, developer, main
Logs page, sorting) and their fixtures live in `web/cypress/logging`. They use
SDK stubs in a dedicated harness, **not** a real Console or LokiStack:

```sh
# Terminal 1
cd web && npm run start:logging-tests
# Terminal 2
cd web && npm run test:logging-cypress
```

For the real Console smoke after opt-in startup, use existing credentials with
appropriate log-view permissions (no test role grants). `OAUTH_URL` is the OAuth
origin, such as `https://oauth-openshift.apps.<cluster>`:

```sh
cd web
export CYPRESS_BASE_URL=https://console-openshift-console.apps.<cluster>
export CYPRESS_LOGIN_USERS='<username>:<password>'
export CYPRESS_LOGIN_IDP='<identity-provider>'
export CYPRESS_OAUTH_URL=https://oauth-openshift.apps.<cluster>
npm run cypress:run -- --spec cypress/e2e/logging/merged-logging.cy.ts \
  --env LOGGING_ENABLED=true,LOGGING_SCHEMA=viaq,LOGGING_TENANT=application
# On the disabled run above:
npm run cypress:run -- --spec cypress/e2e/logging/merged-logging.cy.ts \
  --env LOGGING_ENABLED=false
```

The four ported real Console suites are also available:

```sh
npm run cypress:run -- --spec 'cypress/e2e/logging/*Console*.cy.ts' \
  --env grepTags=@logging,OPENSHIFT_VERSION=4.20,CLUSTERLOGGING_DATAMODE=viaq
```

Use the same Console, IDP, OAuth and kubeconfig variables as above, with
`CYPRESS_LOGIN_USERS='<admin>:<password>,<test-user>:<password>'`. The first
account must already be a cluster admin; the second must be a dedicated user
without infrastructure/audit log access. These suites grant and remove namespace
view/logging/monitoring roles for that test user using `CYPRESS_KUBECONFIG_PATH`.
They do not change the admin account's cluster role or switch CLI credentials.
Run against a disposable test cluster: the running/deleted-pod cases delete
application pods so their controller creates replacements.

Prepare `log-test-app1` and `log-test-app2` with controller-managed
`centos-logtest-*` pods, a `centos-logtest` container emitting `SVTLogger` messages,
and application logs forwarded to Loki. Infrastructure/audit forwarding is
required for the corresponding admin assertions. Match
`CLUSTERLOGGING_DATAMODE=viaq|otel|select` to the merged logging configuration;
set `OPENSHIFT_VERSION` to the cluster's major/minor version. Developer suites
skip when the Developer perspective is unavailable. Two admin Observe Logs
user/impersonation suites retain their upstream `describe.skip` status.

The imported shared cases live in `test-utils.ts`, so Cypress discovers only the
four executable specs. Tags are namespaced (`@logging-admin`, `@logging-user`,
`@logging-observ`, `@logging-aggr`, `@logging-devobserv`, `@logging-common`,
`@logging-smoke`), with `@logging` and `@coo` on every suite. Each test checks
that MCP logging is enabled and rejects requests to the legacy logging proxy.
Live execution of these ported suites remains pending.

Set `CYPRESS_LOGGING_QUERY` to an allowed namespace-scoped LogQL query when the
user cannot read application logs cluster-wide. Repeat with OTEL data and
`LOGGING_SCHEMA=otel`; `schema: select` in config exposes the schema selector.
Screenshots are under `web/cypress/screenshots/` (harness: `logging/`); the normal
Console runner also writes its configured Mochawesome/JUnit reports. The smoke
checks merged manifest/config/proxy identity and successful Loki queries. It does
not replace these manual real-Console parity checks:

- Admin Logs, pod Aggregated Logs, supported developer Logs; user/platform alert
  sources and charts in both supported perspectives.
- ViaQ, OTEL and selectable schema; tenant/namespace filters, histogram, volume,
  pagination, sorting, CSV export, timezone and saved URL/browser state.
- Allowed application/infrastructure/audit queries; denied users remain denied.
- Streaming enabled: inspect a successful WebSocket upgrade/tail, pause/resume,
  and cancellation. Verify empty data, timeout and backend errors.
- Combined MCP features: no duplicate route/source registrations, missing
  translations, `backend` proxy changes, or shared-timeout interference.
- After purge: original operator replicas, Console registrations and Loki proxy.

## Development cutover and operator handoff

After merge, all new logging development belongs in `monitoring-plugin`, even
before production activation. `logging-view-plugin` remains the maintenance
repository for supported standalone releases. Forward-port applicable legacy
fixes, backport applicable merged-feature fixes, and link paired changes or
explain when one implementation is unaffected.

CR consolidation, operator reconciliation, production activation and upgrades are
owned by the separate consolidation ticket (identifier not yet supplied). Its
plugin-side contract is `features=logging`, MCP identity, the authenticated
`loki` alias, and the nested logging config documented here. This change does not
activate logging in existing installations or change their operator API.
