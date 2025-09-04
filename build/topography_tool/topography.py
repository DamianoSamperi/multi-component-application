from flask import Flask, request, jsonify
from kubernetes import client, config
import yaml
import uuid
from typing import Union, List, Dict

app = Flask(__name__)

def flatten_steps(steps: List[Union[Dict, List]], start_id=0):
    flat = []
    current_id = start_id

    def _flatten(substeps, current_id):
        local_flat = []
        step_ids = []  # lista di ID dei substeps

        for step in substeps:
            if isinstance(step, dict):
                step_obj = {
                    "id": current_id,
                    "type": step["type"],
                    "params": step.get("params", {}),
                    "gpu": step.get("gpu", False),
                    "volumes": step.get("volumes", []),
                    "preferred_next": step.get("preferred_next"),
                    "next_step": []
                }
                local_flat.append(step_obj)
                step_ids.append(current_id)
                current_id += 1
            elif isinstance(step, list):
                # flatten ricorsivo della sub-list
                sub_flat, sub_ids, current_id = _flatten(step, current_id)
                local_flat.extend(sub_flat)
                step_ids.extend(sub_ids)

        # assegna next_step
        for idx, step in enumerate(local_flat):
            # se non è l'ultimo, next_step punta al prossimo step
            if idx < len(local_flat) - 1:
                step["next_step"] = [local_flat[idx + 1]["id"]]

        return local_flat, step_ids, current_id

    flat, _, _ = _flatten(steps, current_id)
    return flat


# --- YAML Builders ---
def generate_configmap(step, pipeline_id, namespace="default"):
    return client.V1ConfigMap(
        api_version="v1",
        kind="ConfigMap",
        metadata=client.V1ObjectMeta(
            name=f"{pipeline_id}-step-{step['id']}",
            namespace=namespace,
            labels={"pipeline_id": pipeline_id}
        ),
        data={
            "PIPELINE_CONFIG": yaml.dump({
                "step_id": step["id"],
                "steps": [{
                    "id": step["id"],
                    "type": step["type"],
                    "params": step["params"],
                    **({"next_step": step["next_step"]} if "next_step" in step else {}),
                    **({"preferred_next": step["preferred_next"]} if step.get("preferred_next") else {})
                }]
            }, sort_keys=False)
        }
    )

def generate_deployments(steps: List[Dict], pipeline_prefix: str, namespace="default") -> List[Dict]:
    """
    Genera i deployment per ogni step della pipeline.
    """
    deployments = []

    for step in steps:
        step_id = step["id"]
        deployment_name = f"{pipeline_prefix}-step-{step_id}"

        # container base
        container = {
            "name": "nn",
            "image": "dami00/multicomponent_service",
            "imagePullPolicy": "Always",
            "envFrom": [{"configMapRef": {"name": f"pipeline-step-{step_id}"}}],
            "env": [
                {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}}
            ],
            "ports": [{"containerPort": 5000}],
        }

        # GPU
        if step.get("gpu", False):
            container["resources"] = {"limits": {"nvidia.com/gpu.shared": 1}}

        # Volumes
        volume_mounts = []
        volumes = []
        for vol in step.get("volumes", []):
            if vol == "cuda":
                volume_mounts.append({"mountPath": "/usr/local/cuda", "name": "cuda-volume"})
                volumes.append({"name": "cuda-volume", "hostPath": {"path": "/usr/local/cuda", "type": "Directory"}})
            elif vol == "lib":
                volume_mounts.append({"mountPath": "/usr/lib/aarch64-linux-gnu", "name": "lib-volume"})
                volumes.append({"name": "lib-volume", "hostPath": {"path": "/usr/lib/aarch64-linux-gnu", "type": "Directory"}})
            elif vol == "jetson-inference":
                volume_mounts.append({"mountPath": "/jetson-inference", "name": "jetson-inference-volume"})
                volumes.append({"name": "jetson-inference-volume", "hostPath": {"path": "/home/administrator/jetson-inference", "type": "Directory"}})

        if volume_mounts:
            container["volumeMounts"] = volume_mounts

        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": deployment_name, "namespace": namespace},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "nn-service", "step": str(step_id)}},
                "template": {
                    "metadata": {"labels": {"app": "nn-service", "step": str(step_id)}},
                    "spec": {
                        "containers": [container],
                        "volumes": volumes if volumes else []
                    },
                },
            },
        }

        deployments.append(deployment)

    return deployments


def generate_services(steps: List[Dict], pipeline_prefix: str, namespace="default") -> List[Dict]:
    """
    Genera i service per ogni step della pipeline.
    """
    services = []

    for step in steps:
        step_id = step["id"]
        service_name = f"{pipeline_prefix}-step-{step_id}"

        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": service_name, "namespace": namespace},
            "spec": {
                "selector": {"app": "nn-service", "step": str(step_id)},
                "ports": [{"port": 5000, "targetPort": 5000}],
            },
        }

        services.append(service)

    return services



# --- Endpoints ---
@app.route("/pipeline", methods=["POST"])
def create_pipeline():
    try:
        pipeline = request.get_json()
        if not pipeline or "steps" not in pipeline:
            return jsonify({"error": "Invalid JSON, must contain steps"}), 400

        config.load_incluster_config()
        v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()

        pipeline_id = f"pipeline-{uuid.uuid4().hex[:8]}"

        steps = flatten_steps(pipeline["steps"])
        results = []

        for step in steps:
            cm = generate_configmap(step, pipeline_id)
            dep = generate_deployments(step, pipeline_id)
            svc = generate_service(step, pipeline_id)

            v1.create_namespaced_config_map(namespace="default", body=cm)
            apps_v1.create_namespaced_deployment(namespace="default", body=dep)
            v1.create_namespaced_service(namespace="default", body=svc)

            results.append(f"✅ Creati step {step['id']}")

        return jsonify({"status": "ok", "pipeline_id": pipeline_id, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/pipeline/<pipeline_id>", methods=["DELETE"])
def delete_pipeline(pipeline_id):
    try:
        config.load_incluster_config()
        v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()

        delete_opts = client.V1DeleteOptions()

        # Cancella tutto con label pipeline_id
        apps_v1.delete_collection_namespaced_deployment(namespace="default", label_selector=f"pipeline_id={pipeline_id}", body=delete_opts)
        v1.delete_collection_namespaced_config_map(namespace="default", label_selector=f"pipeline_id={pipeline_id}", body=delete_opts)
        v1.delete_collection_namespaced_service(namespace="default", label_selector=f"pipeline_id={pipeline_id}", body=delete_opts)

        return jsonify({"status": "deleted", "pipeline_id": pipeline_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
