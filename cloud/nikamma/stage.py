"""Stage a validated ytFactory release in a Nikamma checkout; never push or sync."""
import argparse
from pathlib import Path
import re
import shutil

import yaml


REQUIRED_SECRETS = {
    "ytfactory-session": {"YTFACTORY_SESSION_SECRET"},
    "ytfactory-api-secrets": {"YTFACTORY_WEB_OAUTH_CLIENT", "YTFACTORY_AGENT_TOKEN"},
    "ytfactory-google-credentials": {"credentials.json"},
}


def validated_secrets(path):
    documents = list(yaml.safe_load_all(path.read_text()))
    names = set()
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") != "SealedSecret":
            raise ValueError("Only SealedSecret resources may be staged")
        if document.get("apiVersion") != "bitnami.com/v1alpha1":
            raise ValueError("Unexpected SealedSecret API version")
        meta = document.get("metadata", {})
        name = meta.get("name")
        if name not in REQUIRED_SECRETS or name in names or meta.get("namespace") != "ytfactory":
            raise ValueError("Secrets must have unique expected names in the ytfactory namespace")
        spec = document.get("spec", {})
        encrypted = spec.get("encryptedData", {})
        if set(encrypted) & {"K_SERVICE", "YTFACTORY_REQUIRE_AUTH", "YT_AUTH_ENABLED", "YTFACTORY_TOKEN"}:
            raise ValueError("Secrets must not override production authentication flags")
        if not REQUIRED_SECRETS[name].issubset(encrypted) or not all(isinstance(v, str) and v for v in encrypted.values()):
            raise ValueError(f"Missing encrypted keys in {name}")
        template_meta = spec.get("template", {}).get("metadata", {})
        if template_meta.get("name", name) != name or template_meta.get("namespace", "ytfactory") != "ytfactory":
            raise ValueError("Secret template identity must match the sealed resource")
        for value in (document, spec, spec.get("template", {})):
            if "data" in value or "stringData" in value:
                raise ValueError("Plaintext Secret payloads are forbidden")
        names.add(name)
    if names != set(REQUIRED_SECRETS):
        raise ValueError("Provide session, API, and Google credential SealedSecrets")
    return documents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nikamma-checkout", type=Path, required=True)
    parser.add_argument("--sealed-secrets", type=Path, required=True)
    parser.add_argument("--web-image", required=True)
    parser.add_argument("--api-image", required=True)
    args = parser.parse_args()
    images = []
    for service in ("web", "api"):
        image = getattr(args, f"{service}_image")
        match = re.fullmatch(rf"(ghcr\.io/rohitsingh30/ytfactory-{service})@(sha256:[a-f0-9]{{64}})", image)
        if not match:
            parser.error(f"{service} image must be an immutable GHCR sha256 digest")
        images.append({"name": f"ytfactory-{service}", "newName": match[1], "digest": match[2]})
    root = args.nikamma_checkout.resolve()
    if not (root / "AGENTS.md").is_file() or not (root / "argocd/root.yaml").is_file():
        parser.error("Destination must be an existing Nikamma checkout")
    documents = validated_secrets(args.sealed_secrets)
    destination = root / "apps/ytfactory"
    template = Path(__file__).resolve().parent / "manifests"
    # Refuse to overwrite drift: updates should change only image digests unless
    # manifest changes have also been reviewed explicitly.
    if destination.exists():
        parser.error("ytfactory already exists; update its image digests in kustomization.yaml for subsequent releases")
    shutil.copytree(template, destination)
    kustomization = yaml.safe_load((destination / "kustomization.yaml").read_text())
    kustomization["images"] = images
    (destination / "kustomization.yaml").write_text(yaml.safe_dump(kustomization, sort_keys=False))
    (destination / "sealed-secrets.yaml").write_text(yaml.safe_dump_all(documents, sort_keys=False))
    namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "ytfactory", "labels": {"name": "ytfactory"}}}
    (root / "cluster/namespaces/ytfactory.yaml").write_text(yaml.safe_dump(namespace, sort_keys=False))
    application = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": "ytfactory", "namespace": "argocd"},
        "spec": {
            "project": "nikamma",
            "source": {"repoURL": "git@github.com:mukul-mehta/nikamma.git", "targetRevision": "HEAD", "path": "apps/ytfactory"},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "ytfactory"},
            "syncPolicy": {"automated": {"prune": True, "selfHeal": True}, "syncOptions": ["CreateNamespace=true"]},
        },
    }
    (root / "argocd/apps/ytfactory.yaml").write_text(yaml.safe_dump(application, sort_keys=False))
    print(f"Staged release in {destination}; no commit, push, or cluster mutation performed.")


if __name__ == "__main__":
    main()
