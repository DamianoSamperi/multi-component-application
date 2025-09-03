from flask import Flask, request, jsonify
import yaml
from typing import Union, List, Dict
from kubernetes import client, config

app = Flask(__name__)

# Volumi predefiniti
PREDEFINED_VOLUMES = {
    "cuda": {
        "name": "cuda-volume",
        "mountPath": "/usr/local/cuda",
        "hostPath": "/usr/local/cuda"
    },
    "lib": {
        "name": "lib-volume",
        "mountPath": "/usr/lib/aarch64-linux-gnu",
        "hostPath": "/usr/lib/aarch64-linux-gnu"
    },
    "jetson-inference": {
        "name": "jetson-inference-volume",
        "mountPath": "/jetson-inference",
        "hostPath": "/home/administrator/jetson-inference"
    }
}


def flatten_steps(steps: List[Union[Dict, List]], start_id=0):
    flat = []
    current_id = start_id

    for idx, step in enumerate(steps):
        if isinstance(step, dict):
            step_obj = {
                "id": current_id,
                "type": step["type"],
                "params": step.get("params", {}),
                "preferred_next": step.get("preferred_next"),
                "gpu": step.get("gpu", False),
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
                    "gpu": sub.get("gpu", False),
                    "volumes": sub.get("volumes", []),
                    "next_step": []
                }
                flat.append(step_obj)
                current_id += 1

    return flat


def generate_configmap(step, namespace="default"):
    return client.V1ConfigMap(
        api_version="v1",
        kind="ConfigMap",
        metadata=client.V1ObjectMeta(
            name=f"pipeline-step-{step['id']}", namespace=namespace
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


def generate_deployment(step, namespace="default"):
    volume_mounts, volumes = [], []

    for v in step.get("volumes", []):
        if v in PREDEFINED_VOLUMES:
            vol = PREDEFINED_VOLUMES[v]
            volume_mounts.append(client.V1VolumeMount(
                name=vol["name"], mount_path=vol["mountPath"]
            ))
            volumes.append(client.V1Volume(
                name=vol["name"],
                host_path=client.V1HostPathVolumeSource(path=vol["hostPath"])
            ))

    resources = None
    if step.get("gpu", False):
        resources = client.V1ResourceRequirements(
            limits={"nvidia.com/gpu.shared": "1"}
        )

    container = client.V1Container(
        name="nn",
        image="dami00/multicomponent_service",
        image_pull_policy="Always",
        env_from=[client.V1EnvFromSource(config_map_ref=client.V1ConfigMapEnvSource(
            name=f"pipeline-step-{step['id']}"))],
        env=[client.V1EnvVar(
            name="POD_NAMESPACE",
            value_from=client.V1EnvVarSource(
                field_ref=client.V1ObjectFieldSelector(field_path="metadata.namespace")
            )
        )],
        ports=[client.V1ContainerPort(container_port=5000)],
        resources=resources,
        volume_mounts=volume_mounts
    )

    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"app": "nn-service", "step": str(step['id'])}),
        spec=client.V1PodSpec(containers=[container], volumes=volumes)
    )

    spec = client.V1DeploymentSpec(
        replicas=1,
        selector=client.V1LabelSelector(match_labels={"app": "nn-service", "step": str(step['id'])}),
        template=template
    )

    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=f"nn-step-{step['id']}", namespace=namespace),
        spec=spec
    )


def generate_service(step, namespace="default"):
    return client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(name=f"nn-step-{step['id']}", namespace=namespace),
        spec=client.V1ServiceSpec(
            selector={"app": "nn-service", "step": str(step['id'])},
            ports=[client.V1ServicePort(port=5000, target_port=5000)]
        )
    )


@app.route("/pipeline", methods=["POST"])
def update_pipeline():
    try:
        pipeline = request.get_json()
        if not pipeline:
            return jsonify({"error": "Invalid JSON"}), 400

        config.load_incluster_config()
        v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()

        steps = flatten_steps(pipeline["steps"])
        results = []

        for step in steps:
            cm = generate_configmap(step)
            dep = generate_deployment(step)
            svc = generate_service(step)

            v1.create_namespaced_config_map(namespace="default", body=cm)
            apps_v1.create_namespaced_deployment(namespace="default", body=dep)
            v1.create_namespaced_service(namespace="default", body=svc)

            results.append(f"✅ Creati step {step['id']}")

        return jsonify({"status": "ok", "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
