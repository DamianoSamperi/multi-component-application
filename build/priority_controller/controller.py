import os
import time
import requests
import yaml
from kubernetes import client, config
from datetime import datetime

# ===== CONFIG =====
PROM_URL = os.getenv("PROMETHEUS_URL", "http://prometheus.monitoring.svc.cluster.local:9090")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))
NAMESPACE = os.getenv("PIPELINE_NAMESPACE", "default")
LOW_PRIORITY_CLASS = os.getenv("LOW_PRIORITY_CLASS", "low-qos")
MEDIUM_PRIORITY_CLASS = os.getenv("MEDIUM_PRIORITY_CLASS", "medium-qos")
HIGH_PRIORITY_CLASS = os.getenv("HIGH_PRIORITY_CLASS", "high-qos")
PRIORITY_THRESHOLDS = []
PRIORITY_THRESHOLDS_LAST_RELOAD = 0
PRIORITY_THRESHOLDS_RELOAD_INTERVAL = 300  # 5 minuti
PRIORITY_COOLDOWN = 60  # secondi 
PRIORITY_DOWNSCALE_GRACE = 600  # 10 minuti
DOWNSCALE_ZERO_REQUIRED = 3
ZERO_COUNT = {}
LAST_NONZERO_INFLIGHT = {}
LAST_PRIORITY_CHANGE = {}
# ===== SETUP =====
try:
    config.load_incluster_config()
except:
    config.load_kube_config()

v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()

print(f"[INFO] Priority Controller avviato - Prometheus={PROM_URL}, Namespace={NAMESPACE}", flush=True)

def load_priority_thresholds():
    """
    Legge le PriorityClass e costruisce una lista tipo:
    [
      {"name": "low-qos", "min": 0, "max": 20},
      {"name": "medium-qos", "min": 21, "max": 60},
      {"name": "high-qos", "min": 61, "max": 100000},
    ]
    """
    api = client.SchedulingV1Api()
    pcs = api.list_priority_class().items

    thresholds = []

    for pc in pcs:
        if pc.metadata.name.startswith("system-"):
            continue
        ann = pc.metadata.annotations or {}
        try:
            if "qos.threshold.min_inflight" not in ann or "qos.threshold.max_inflight" not in ann:
                continue
            min_v = int(ann.get("qos.threshold.min_inflight", "0"))
            max_v = int(ann.get("qos.threshold.max_inflight", "0"))
            thresholds.append({
                "name": pc.metadata.name,
                "min": min_v,
                "max": max_v
            })
        except ValueError:
            continue

    # ordina per min crescente
    thresholds.sort(key=lambda x: x["min"])
    print(f"[INFO] QoS thresholds caricati: {thresholds}", flush=True)
    return thresholds
def refresh_priority_thresholds_if_needed():
    global PRIORITY_THRESHOLDS, PRIORITY_THRESHOLDS_LAST_RELOAD
    now = time.time()
    if not PRIORITY_THRESHOLDS or (now - PRIORITY_THRESHOLDS_LAST_RELOAD) > PRIORITY_THRESHOLDS_RELOAD_INTERVAL:
        PRIORITY_THRESHOLDS = load_priority_thresholds()
        PRIORITY_THRESHOLDS_LAST_RELOAD = now

def notify_pod_drain(pod_ip):
    try:
        url = f"http://{pod_ip}:5000/drain"
        resp = requests.post(url, timeout=5)
        if resp.status_code == 200:
            print(f"[INFO] Pod {pod_ip} in draining")
        else:
            print(f"[WARN] Pod {pod_ip} ha risposto {resp.status_code}")
    except Exception as e:
        print(f"[WARN] Errore durante notifica drain a {pod_ip}: {e}")
def in_cooldown(pipeline_id, step_id):
    ts = LAST_PRIORITY_CHANGE.get((pipeline_id, step_id))
    if not ts:
        return False
    return (time.time() - ts) < PRIORITY_COOLDOWN
    
def wait_for_pod_not_ready(pod_name, timeout_s=180):
    start = time.time()
    while True:
        try:
            pod = v1.read_namespaced_pod(pod_name, namespace=NAMESPACE)
        except Exception:
            return  # pod sparito

        for cond in pod.status.conditions or []:
            if cond.type == "Ready" and cond.status == "False":
                return

        if time.time() - start > timeout_s:
            print(f"[WARN] Timeout waiting NotReady for {pod_name}", flush=True)
            return

        time.sleep(1)

# ===== GPU FACTORS (QoSAware) =====

FALLBACK_GPU_FACTOR = {
    "nano": 1,
    "xavier": 2,
    "orin": 3,
}

PRIORITY_MIN_FACTOR = {
    HIGH_PRIORITY_CLASS: 3,
    MEDIUM_PRIORITY_CLASS: 2,
    LOW_PRIORITY_CLASS: 1,
}


GPU_LABEL_KEY = "nvidia.com/device-plugin.config"

GPU_FACTOR = {}
GPU_FACTOR_LAST_RELOAD = 0
GPU_FACTOR_RELOAD_INTERVAL = 300  # secondi


def load_gpu_factors_from_scheduler():
    try:
        cm = v1.read_namespaced_config_map(
            name="scheduler-config",
            namespace="scheduler-plugins"
        )

        raw = cm.data.get("scheduler-config.yaml")
        if not raw:
            raise ValueError("scheduler-config.yaml missing")

        cfg = yaml.safe_load(raw)

        for pc in cfg.get("pluginConfig", []):
            if pc.get("name") == "QoSAware":
                mappings = pc.get("args", {}).get("mappings", [])
                factors = {
                    m["labelValue"]: float(m["factor"])
                    for m in mappings
                }
                print(f"[INFO] GPU_FACTOR caricata da scheduler: {factors}", flush=True)
                return factors

        raise ValueError("QoSAware config not found")

    except Exception as e:
        print(f"[WARN] Uso fallback GPU_FACTOR: {e}", flush=True)
        return FALLBACK_GPU_FACTOR.copy()


def refresh_gpu_factors_if_needed():
    global GPU_FACTOR, GPU_FACTOR_LAST_RELOAD
    now = time.time()
    if not GPU_FACTOR or (now - GPU_FACTOR_LAST_RELOAD) > GPU_FACTOR_RELOAD_INTERVAL:
        GPU_FACTOR = load_gpu_factors_from_scheduler()
        GPU_FACTOR_LAST_RELOAD = now


def pod_already_on_suitable_node(pod, new_priority):
    refresh_gpu_factors_if_needed()

    node_name = pod.spec.node_name
    if not node_name:
        return False

    node = v1.read_node(node_name)
    gpu_class = node.metadata.labels.get(GPU_LABEL_KEY)
    if not gpu_class:
        return False

    node_factor = GPU_FACTOR.get(gpu_class, 0)
    required_factor = PRIORITY_MIN_FACTOR.get(new_priority, 0)

    return node_factor >= required_factor

def query_prometheus(query: str):
    """Esegue una query Prometheus e restituisce i risultati come lista di dict."""
    url = f"{PROM_URL}/api/v1/query"
    try:
        resp = requests.get(url, params={"query": query}, timeout=10)
        data = resp.json()
        if data["status"] == "success" and data["data"]["result"]:
            return data["data"]["result"]  # lista di metriche
    except Exception as e:
        print(f"[WARN] Errore query Prometheus: {e}", flush=True)
    return []

def restart_deployment_for_step(pipeline_id, step_id):
    deployment_name = f"{pipeline_id}-step-{step_id}"
    try:
        apps_v1.patch_namespaced_deployment(
            name=deployment_name,
            namespace=NAMESPACE,
            body={
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "restarted-at": datetime.utcnow().isoformat()
                            }
                        }
                    }
                }
            }
        )
        print(f"[ROLLING] Riavviato deployment {deployment_name} con nuova priority", flush=True)
    except Exception as e:
        print(f"[WARN] Impossibile riavviare {deployment_name}: {e}", flush=True)
        
def get_all_pipelines():
    """Ritorna tutte le configmap con label pipeline_id."""
    cms = v1.list_namespaced_config_map(namespace=NAMESPACE, label_selector="pipeline_id")
    return cms.items
def get_pods_for_step(pipeline_id, step_id):
    """Ritorna la lista dei pod per un dato step e pipeline."""
    label_selector = f"app=nn-service,pipeline_id={pipeline_id},step={step_id}"
    pods = v1.list_namespaced_pod(namespace=NAMESPACE, label_selector=label_selector)
    return pods.items

def update_configmap_priority(cm_name, new_priority, step_id):
    cm = v1.read_namespaced_config_map(name=cm_name, namespace=NAMESPACE)
    data = yaml.safe_load(cm.data["PIPELINE_CONFIG"])

    pipeline_id = cm.metadata.labels["pipeline_id"]
    key = (pipeline_id, step_id)

    #  COOLDOWN CHECK – SUBITO
    if in_cooldown(*key):
        print(
            f"[COOLDOWN] Skip priority change for {key}, still in cooldown",
            flush=True
        )
        return

    updated = False

    #  DECISIONE
    for step in data.get("steps", []):
        if step["id"] == step_id:
            if step["priority"] == new_priority:
                return
            step["priority"] = new_priority
            updated = True

    if not updated:
        return

    # COMMIT CONFIGMAP
    cm.data["PIPELINE_CONFIG"] = yaml.dump(data)
    v1.replace_namespaced_config_map(
        name=cm_name,
        namespace=NAMESPACE,
        body=cm
    )
    apps_v1.patch_namespaced_deployment(
    name=f"{pipeline_id}-step-{step_id}",
    namespace=NAMESPACE,
    body={
            "spec": {
                "template": {
                    "spec": {
                        "priorityClassName": new_priority
                    }
                }
            }
        }
    )

    LAST_PRIORITY_CHANGE[key] = time.time()

    print(
        f"[UPDATE] ConfigMap {cm_name} aggiornata con priority={new_priority} per step {step_id}",
        flush=True
    )

    # DRAIN + ROLLOUT
    pods = get_pods_for_step(pipeline_id=pipeline_id, step_id=step_id)
    pod_to_drain = None
    for pod in pods:
        if pod.status.phase != "Running":
            continue
        for cond in pod.status.conditions or []:
            if cond.type == "Ready" and cond.status == "True":
                pod_to_drain = pod
                break

        if pod_to_drain:
            break

    if not pod_to_drain:
        print("[INFO] Nessun pod READY da drainare", flush=True)
    else:
        if pod_already_on_suitable_node(pod_to_drain, new_priority):
            print(
                f"[SKIP] Pod {pod_to_drain.metadata.name} già su nodo adeguato "
                f"(priority={new_priority})",
                flush=True
            )
            return
    
        if pod_to_drain.status.pod_ip:
            notify_pod_drain(pod_to_drain.status.pod_ip)
            wait_for_pod_not_ready(pod_to_drain.metadata.name)

    # restart_deployment_for_step(pipeline_id=pipeline_id, step_id=step_id)


def choose_priority(in_flight: float) -> str:
    refresh_priority_thresholds_if_needed()

    for entry in PRIORITY_THRESHOLDS:
        if entry["min"] <= in_flight <= entry["max"]:
            return entry["name"]

    # fallback di sicurezza
    return LOW_PRIORITY_CLASS

def evaluate_priority():
    """Analizza le metriche Prometheus e aggiorna priorità solo per gli step interessati."""
    #query = 'sum(rate(http_requests_total{namespace="default"}[1m])) by (pipeline_id, step_id)'
    # query = '''
    # sum(
    #   max_over_time(
    #     http_requests_in_progress[30s]
    #   )
    # )
    # by (pipeline_id, step_id)
    # '''
    query = '''
    sum by (pipeline_id, step_id) (
      max_over_time(
        http_requests_in_progress{job=~"pipeline-.*"}[30s]
      )
    )
    '''
    results = query_prometheus(query)

    if not results:
        print("[WARN] Nessuna metrica disponibile, salto iterazione.", flush=True)
        return

    for metric in results:
        pipeline_id = metric["metric"]["pipeline_id"]
        step_id = int(metric["metric"]["step_id"])
        key = (pipeline_id, step_id)
        if in_cooldown(*key):
            continue
        # in_flight = float(metric["value"][1])
        in_flight = max(0.0, float(metric["value"][1]))
        
        # if in_flight > 0:
        #     LAST_NONZERO_INFLIGHT[(pipeline_id, step_id)] = time.time()
        if in_flight == 0:
            ZERO_COUNT[key] = ZERO_COUNT.get(key, 0) + 1
        else:
            ZERO_COUNT[key] = 0
        new_priority = choose_priority(in_flight)
        if in_flight > 0 and new_priority != LOW_PRIORITY_CLASS:
            LAST_NONZERO_INFLIGHT[(pipeline_id, step_id)] = time.time()
        print(
            f"[DECISION] pipeline={pipeline_id} step={step_id} "
            f"in_flight={in_flight:.2f} → target_priority={new_priority}",
            flush=True
        )
        # Cerca la ConfigMap corrispondente a questa pipeline
        # cms = v1.list_namespaced_config_map(namespace=NAMESPACE, label_selector=f"pipeline_id={pipeline_id}").items
        # for cm in cms:
        #     update_configmap_priority(cm.metadata.name, new_priority, step_id)
        # print(
        #     f"[INFO] Pipeline={pipeline_id} Step={step_id} "
        #     f"in_flight={in_flight:.1f} → priority={new_priority}",
        #     flush=True
        # )
        if new_priority == LOW_PRIORITY_CLASS and ZERO_COUNT.get(key, 0) < DOWNSCALE_ZERO_REQUIRED:
            print(f"[HYSTERESIS] Skip downscale for {key}, zero_count={ZERO_COUNT.get(key, 0)}", flush=True)
            continue

        if new_priority == LOW_PRIORITY_CLASS:
            last_active = LAST_NONZERO_INFLIGHT.get((pipeline_id, step_id))
            if last_active and (time.time() - last_active) < PRIORITY_DOWNSCALE_GRACE:
                print(
                    f"[STICKY] Skip downscale for {(pipeline_id, step_id)} "
                    f"(recent activity)",
                    flush=True
                )
                continue
        cm_name = f"{pipeline_id}-step-{step_id}"
        update_configmap_priority(cm_name, new_priority, step_id)


def main():
    while True:
        try:
            evaluate_priority()
        except Exception as e:
            print(f"[ERROR] Errore nel ciclo principale: {e}", flush=True)
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
