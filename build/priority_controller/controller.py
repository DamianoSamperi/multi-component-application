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

print(f"[INFO] Priority Controller avviato - Prometheus={PROM_URL}, Namespace={NAMESPACE}")

def query_prometheus(query: str):
    """Esegue una query Prometheus e restituisce il risultato numerico medio."""
    url = f"{PROM_URL}/api/v1/query"
    try:
        resp = requests.get(url, params={"query": query}, timeout=10)
        data = resp.json()
        if data["status"] == "success" and data["data"]["result"]:
            values = [float(r["value"][1]) for r in data["data"]["result"]]
            return sum(values) / len(values)
    except Exception as e:
        print(f"[WARN] Errore query Prometheus: {e}")
    return None

def get_all_pipelines():
    """Ritorna tutte le configmap con label pipeline_id."""
    cms = v1.list_namespaced_config_map(namespace=NAMESPACE, label_selector="pipeline_id")
    return cms.items

def update_configmap_priority(cm_name, new_priority):
    """Aggiorna la priority in una ConfigMap."""
    cm = v1.read_namespaced_config_map(name=cm_name, namespace=NAMESPACE)
    data = yaml.safe_load(cm.data["PIPELINE_CONFIG"])

    # Aggiorna il campo priority
    for step in data.get("steps", []):
        step["priority"] = new_priority

    cm.data["PIPELINE_CONFIG"] = yaml.dump(data)
    v1.replace_namespaced_config_map(name=cm_name, namespace=NAMESPACE, body=cm)
    print(f"[UPDATE] ConfigMap {cm_name} aggiornata con priority={new_priority}")

def evaluate_priority():
    """Analizza le metriche Prometheus e aggiorna priorità."""
    # Query d'esempio: RPS medio per tutti i pod di nn-service
    query = 'sum(rate(http_requests_total{app="nn-service"}[1m])) by (pod)'
    avg_rps = query_prometheus(query)

    if avg_rps is None:
        print("[WARN] Nessuna metrica disponibile, salto iterazione.")
        return

    print(f"[INFO] RPS medio globale: {avg_rps:.2f}")

    new_priority = HIGH_PRIORITY_CLASS if avg_rps > THRESHOLD_RPS else LOW_PRIORITY_CLASS

    # Aggiorna tutte le ConfigMap pipeline
    for cm in get_all_pipelines():
        update_configmap_priority(cm.metadata.name, new_priority)

def main():
    while True:
        try:
            evaluate_priority()
        except Exception as e:
            print(f"[ERROR] Errore nel ciclo principale: {e}")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
