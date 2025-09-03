from flask import Flask, request, jsonify
from kubernetes import client, config
import yaml
from typing import Union, List, Dict

app = Flask(__name__)

# Inizializza la connessione al cluster
config.load_incluster_config()  # se gira dentro Kubernetes
v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()
rbac_v1 = client.RbacAuthorizationV1Api()

def flatten_steps(steps: List[Union[Dict, List]], start_id=0):
    """ Trasforma lo schema JSON semplificato in una lista lineare di step """
    flat = []
    current_id = start_id

    for idx, step in enumerate(steps):
        if isinstance(step, dict):
            step_obj = {
                "id": current_id,
                "type": step["type"],
                "params": step.get("params", {}),
                "preferred_next": step.get("preferred_next"),
                "needs_gpu": step.get("needs_gpu", False),
                "volumes": step.get("volumes", [])
            }

            if idx < len(steps) - 1:
                if isinstance(steps[idx + 1], list):
                    next_ids = list(range(current_id + 1,
                                          current_id + 1 + len(steps[idx + 1])))
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
                    "next_step": [],
                    "needs_gpu": sub.get("needs_gpu", False),
                    "volumes": sub.get("volumes", [])
                }
                flat.append(step_obj)
                current_id += 1

    return flat

def create_configmap(step: Dict, namespace="default"):
    name = f"pipeline-step-{step['id']}"
    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        data={"PIPELINE_CONFIG": yaml.dump({
            "step_id": step["id"],
            "steps": [{
                "id": step["id"],
                "type": step["type"],
                "params": step["params"],
                **({"next_step": step["next_step"]} if "next_step" in step else {}),
                **({"preferred_next": step["preferred_next"]} if step.get("preferred_next") else {})
            }]
        }, sort_keys=False)}
    )
    try:
        v1.create_namespaced_config_map(namespace, cm)
    except client.exceptions.ApiException as e:
        if e.status == 409:  # già esiste
            v1.replace_namespaced_config_map(name, namespace, cm)
        else:
            raise
    return name

def create_deployment(step: Dict, image="dami00/multicomponent_service",
                      namespace="default"):
    name = f"nn-step-{step['id']}"
    container = client.V1Container(
        name="nn",
        image=image,
        image_pull_policy="Always",
        env=[client.V1EnvVar(
            name="POD_NAMESPACE",
            value_from=client.V1EnvVarSource(
                field_ref=client.V1ObjectFieldSelector(field_path="metadata.namespace")
            )
        )],
        env_from=[client.V1EnvFromSource(
            config_map_ref=client.V1ConfigMapEnvSource(name=f"pipeline-step-{step['id']}")
        )],
        ports=[client.V1ContainerPort(container_port=5000)]
    )

    # Risorse GPU se richieste
    if step.get("needs_gpu"):
        container.resources = client.V1ResourceRequirements(
            limits={"nvidia.com/gpu.shared": "1"}
        )

    # Volumi se presenti
    volume_mounts = []
    volumes = []
    for vol in step.get("volumes", []):
        mount = client.V1VolumeMount(
            name=vol["name"],
            mount_path=vol["mountPath"]
        )
        volume_mounts.append(mount)
        v = client.V1Volume(
            name=vol["name"],
            host_path=client.V1HostPathVolumeSource(path=vol["hostPath"])
        )
        volumes.append(v)
    container.volume_mounts = volume_mounts

    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"app": "nn-service", "step": str(step["id"])}),
        spec=client.V1PodSpec(containers=[container], volumes=volumes)
    )

    spec = client.V1DeploymentSpec(
        replicas=1,
        selector=client.V1LabelSelector(match_labels={"app": "nn-service", "step": str(step["id"])}),
        template=template
    )

    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=spec
    )

    try:
        apps_v1.create_namespaced_deployment(namespace, deployment)
    except client.exceptions.ApiException as e:
        if e.status == 409:
            apps_v1.replace_namespaced_deployment(name, namespace, deployment)
        else:
            raise

    # Crea anche un Service ClusterIP
    service = client.V1Service(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=client.V1ServiceSpec(
            selector={"app": "nn-service", "step": str(step["id"])},
            ports=[client.V1ServicePort(port=5000, target_port=5000)]
        )
    )
    try:
        v1.create_namespaced_service(namespace, service)
    except client.exceptions.ApiException as e:
        if e.status == 409:
            v1.replace_namespaced_service(name, namespace, service)
        else:
            raise

@app.route("/pipeline", methods=["POST"])
def update_pipeline():
    try:
        pipeline = request.get_json()
        if not pipeline:
            return jsonify({"error": "Invalid JSON"}), 400

        steps = flatten_steps(pipeline["steps"])
        results = []

        for step in steps:
            cm_name = create_configmap(step)
            create_deployment(step)
            results.append(f"Step {step['id']} applied with ConfigMap {cm_name}")

        return jsonify({"status": "ok", "results": results})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
