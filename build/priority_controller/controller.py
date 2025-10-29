import os
import time
import requests
import yaml
from kubernetes import client, config

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

def get_all_pipelines():
    """Ritorna tutte le configmap con label pipeline_id."""
    cms = v1.list_namespaced_config_map(namespace=NAMESPACE, label_selector="pipeline_id")
    return cms.items

def update_configmap_priority(cm_name, new_priority, step_id):
    """Aggiorna la priority in uno specifico step di una ConfigMap."""
    cm = v1.read_namespaced_config_map(name=cm_name, namespace=NAMESPACE)
    data = yaml.safe_load(cm.data["PIPELINE_CONFIG"])

    updated = False
    for step in data.get("steps", []):
        if step["id"] == step_id:
            step["priority"] = new_priority
            updated = True

    if updated:
        cm.data["PIPELINE_CONFIG"] = yaml.dump(data)
        v1.replace_namespaced_config_map(name=cm_name, namespace=NAMESPACE, body=cm)
        print(f"[UPDATE] ConfigMap {cm_name} aggiornata con priority={new_priority} per step {step_id}", flush=True)

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
