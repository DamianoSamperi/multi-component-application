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
THRESHOLD_RPS = float(os.getenv("THRESHOLD_RPS", "5.0"))  # soglia
LOW_PRIORITY_CLASS = os.getenv("LOW_PRIORITY_CLASS", "low-qos")
HIGH_PRIORITY_CLASS = os.getenv("HIGH_PRIORITY_CLASS", "high-qos")

# ===== SETUP =====
try:
    config.load_incluster_config()
except:
    config.load_kube_config()

v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()

print(f"[INFO] Priority Controller avviato - Prometheus={PROM_URL}, Namespace={NAMESPACE}", flush=True)


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

def wait_for_pod_not_ready(pod_name):
    """Attende che il pod diventi NotReady."""
    while True:
        pod = v1.read_namespaced_pod(pod_name, namespace=NAMESPACE)
        for cond in pod.status.conditions or []:
            if cond.type == "Ready" and cond.status == "False":
                return
        time.sleep(1)
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
    """Aggiorna la priority in uno specifico step di una ConfigMap."""
    cm = v1.read_namespaced_config_map(name=cm_name, namespace=NAMESPACE)
    data = yaml.safe_load(cm.data["PIPELINE_CONFIG"])

    updated = False
    for step in data.get("steps", []):
        if step["id"] == step_id:
            if step["priority"] == new_priority:
                return
            step["priority"] = new_priority
            updated = True

    if updated:
        cm.data["PIPELINE_CONFIG"] = yaml.dump(data)
        v1.replace_namespaced_config_map(name=cm_name, namespace=NAMESPACE, body=cm)
        print(f"[UPDATE] ConfigMap {cm_name} aggiornata con priority={new_priority} per step {step_id}", flush=True)
        # Dopo aver aggiornato la ConfigMap, notifichiamo i pod attivi
        pods = get_pods_for_step(pipeline_id=cm.metadata.labels["pipeline_id"], step_id=step_id)
        for pod in pods:
            if pod.status.pod_ip:
                notify_pod_drain(pod.status.pod_ip)
                wait_for_pod_not_ready(pod.metadata.name)
        restart_deployment_for_step(pipeline_id=cm.metadata.labels["pipeline_id"], step_id=step_id)


def evaluate_priority():
    """Analizza le metriche Prometheus e aggiorna priorità solo per gli step interessati."""
    query = 'sum(rate(http_requests_total{namespace="default"}[1m])) by (pipeline_id, step_id)'
    results = query_prometheus(query)

    if not results:
        print("[WARN] Nessuna metrica disponibile, salto iterazione.", flush=True)
        return

    for metric in results:
        pipeline_id = metric["metric"]["pipeline_id"]
        step_id = int(metric["metric"]["step_id"])
        rps = float(metric["value"][1])

        new_priority = HIGH_PRIORITY_CLASS if rps > THRESHOLD_RPS else LOW_PRIORITY_CLASS

        # Cerca la ConfigMap corrispondente a questa pipeline
        cms = v1.list_namespaced_config_map(namespace=NAMESPACE, label_selector=f"pipeline_id={pipeline_id}").items
        for cm in cms:
            update_configmap_priority(cm.metadata.name, new_priority, step_id)
        print(f"[INFO] Step {step_id} della pipeline {pipeline_id} - RPS={rps:.2f}, priority={new_priority}", flush=True)

def main():
    while True:
        try:
            evaluate_priority()
        except Exception as e:
            print(f"[ERROR] Errore nel ciclo principale: {e}", flush=True)
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
