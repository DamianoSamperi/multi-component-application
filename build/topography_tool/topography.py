from flask import Flask, request, jsonify
from kubernetes import client, config
import yaml
import uuid
from typing import Union, List, Dict

app = Flask(__name__)

# --- Flatten Steps ---
def flatten_steps(steps: List[Union[Dict, List]], start_id=0):
    """
    Trasforma lo schema JSON semplificato in lista di step con id e next_step.
    """
    flat = []
    current_id = start_id

    for idx, step in enumerate(steps):
        if isinstance(step, dict):
            step_obj = {
                "id": current_id,
                "type": step["type"],
                "params": step.get("params", {}),
                "gpu": step.get("gpu", False),
                "volumes": step.get("volumes", []),
                "preferred_next": step.get("preferred_next")
            }
            if idx < len(steps) - 1:
                if isinstance(steps[idx + 1], list):
                    next_ids = list(range(current_id + 1, current_id + 1 + len(steps[idx + 1])))
                else:
                    next_ids = [current_id + 1]
                step_obj["next_step"] = next_ids
            flat.append(step_obj)
            current_id += 1
        elif isinstance(step, list):
            for sub in step:
                step_obj = {
                    "id": current_id,
                    "type": sub["type"],
                    "params": sub.get("params", {}),
                    "gpu": sub.get("gpu", False),
                    "volumes": sub.get("volumes", [])
                }
                step_obj["next_step"] = []
                flat.append(step_obj)
                current_id += 1
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


def generate_deployment(step, pipeline_id, namespace="default"):
    volumes, volume_mounts = [], []

    # Gestione volumi
    for vol in step.get("volumes", []):
        if vol == "cuda":
            volumes.append(client.V1Volume(
                name="cuda-volume",
                host_path=client.V1HostPathVolumeSource(path="/usr/local/cuda", type="Directory")
            ))
            volume_mounts.append(client.V1VolumeMount(mount_path="/usr/local/cuda", name="cuda-volume"))
        elif vol == "lib":
            volumes.append(client.V1Volume(
                name="lib-volume",
                host_path=client.V1HostPathVolumeSource(path="/usr/lib/aarch64-linux-gnu", type="Directory")
            ))
            volume_mounts.append(client.V1VolumeMount(mount_path="/usr/lib/aarch64-linux-gnu", name="lib-volume"))
        elif vol == "jetson-inference":
            volumes.append(client.V1Volume(
                name="jetson-inference-volume",
                host_path=client.V1HostPathVolumeSource(path="/home/administrator/jetson-inference", type="Directory")
            ))
            volume_mounts.append(client.V1VolumeMount(mount_path="/jetson-inference", name="jetson-inference-volume"))

    resources = client.V1ResourceRequirements(
        limits={"nvidia.com/gpu.shared": "1"} if step.get("gpu") else {}
    )

    container = client.V1Container(
        name="nn",
        image="dami00/multicomponent_service",
        image_pull_policy="Always",
        env_from=[client.V1EnvFromSource(config_map_ref=client.V1ConfigMapEnvSource(name=f"{pipeline_id}-step-{step['id']}"))],
        env=[client.V1EnvVar(name="POD_NAMESPACE", value_from=client.V1EnvVarSource(field_ref=client.V1ObjectFieldSelector(field_path="metadata.namespace")))],
        ports=[client.V1ContainerPort(container_port=5000)],
        resources=resources,
        volume_mounts=volume_mounts
    )

    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"app": "nn-service", "step": str(step['id']), "pipeline_id": pipeline_id}),
        spec=client.V1PodSpec(containers=[container], volumes=volumes)
    )

    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=f"{pipeline_id}-step-{step['id']}", namespace=namespace, labels={"pipeline_id": pipeline_id}),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels={"app": "nn-service", "step": str(step['id']), "pipeline_id": pipeline_id}),
            template=template
        )
    )


def generate_service(step, pipeline_id, namespace="default"):
    return client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(
            name=f"{pipeline_id}-step-{step['id']}",
            namespace=namespace,
            labels={"pipeline_id": pipeline_id}
        ),
        spec=client.V1ServiceSpec(
            selector={"app": "nn-service", "step": str(step['id']), "pipeline_id": pipeline_id},
            ports=[client.V1ServicePort(port=5000, target_port=5000)]
        )
    )


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
            dep = generate_deployment(step, pipeline_id)
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
