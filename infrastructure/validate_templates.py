"""Structural validation for the FleetMate CloudFormation/SAM templates.

SAM CLI and cfn-lint are not available in this environment, so this script loads
each template with a PyYAML loader that tolerates CloudFormation short-form
intrinsic tags (``!Ref``, ``!GetAtt``, ``!Sub``, ``!If``, ``!Equals``, ...),
then asserts the essential CloudFormation structure is well formed:

* the document parses as a mapping,
* a ``Resources`` section exists with at least one resource,
* every resource is a mapping declaring a string ``Type``,
* nested-stack and parameter-file references resolve on disk,
* JSON parameter files parse and expose ``ParameterKey``/``ParameterValue``.

Run: ``python infrastructure/validate_templates.py``
Exit code 0 means every template is structurally valid.
"""

from __future__ import annotations

import json
import pathlib
import sys

import yaml

# CloudFormation intrinsic tags come in scalar, sequence, and mapping forms.
# Register a multi-constructor so the SafeLoader accepts any "!Xxx" tag and
# preserves the node's value, letting us validate structure without resolving
# the intrinsics (which only CloudFormation can do at deploy time).
_CFN_TAGS = (
    "!Ref", "!GetAtt", "!Sub", "!Join", "!Select", "!Split", "!GetAZs",
    "!ImportValue", "!FindInMap", "!Base64", "!Cidr", "!If", "!Not", "!And",
    "!Or", "!Equals", "!Condition",
)


class CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form intrinsic tags."""


def _construct_cfn_tag(loader: yaml.Loader, tag_suffix: str, node):
    if isinstance(node, yaml.ScalarNode):
        return {tag_suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {tag_suffix: loader.construct_sequence(node)}
    return {tag_suffix: loader.construct_mapping(node)}


# Register every known CloudFormation intrinsic tag.
for _tag in _CFN_TAGS:
    CfnLoader.add_constructor(
        _tag, lambda loader, node, _t=_tag: _construct_cfn_tag(loader, _t, node)
    )


def validate_template(path: pathlib.Path) -> list[str]:
    """Return a list of structural errors (empty => valid)."""

    errors: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            doc = yaml.load(handle, Loader=CfnLoader)
    except yaml.YAMLError as exc:  # malformed YAML
        return [f"{path.name}: YAML parse error: {exc}"]

    if not isinstance(doc, dict):
        return [f"{path.name}: top-level document is not a mapping"]

    resources = doc.get("Resources")
    if not isinstance(resources, dict) or not resources:
        errors.append(f"{path.name}: missing or empty 'Resources' section")
    else:
        for name, resource in resources.items():
            if not isinstance(resource, dict):
                errors.append(f"{path.name}: resource '{name}' is not a mapping")
                continue
            rtype = resource.get("Type")
            if not isinstance(rtype, str):
                errors.append(
                    f"{path.name}: resource '{name}' has no string 'Type'"
                )
    return errors


def validate_nested_refs(root: pathlib.Path) -> list[str]:
    """Check nested-stack TemplateURLs resolve relative to the root template."""

    errors: list[str] = []
    with root.open("r", encoding="utf-8") as handle:
        doc = yaml.load(handle, Loader=CfnLoader)
    for name, resource in (doc.get("Resources") or {}).items():
        if resource.get("Type") == "AWS::CloudFormation::Stack":
            url = (resource.get("Properties") or {}).get("TemplateURL")
            if isinstance(url, str):
                target = (root.parent / url).resolve()
                if not target.exists():
                    errors.append(
                        f"{root.name}: nested stack '{name}' TemplateURL not found: {url}"
                    )
    return errors


def validate_params(path: pathlib.Path) -> list[str]:
    """Validate a per-environment parameter JSON file."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{path}: JSON parse error: {exc}"]
    if not isinstance(data, list) or not data:
        return [f"{path}: expected a non-empty list of parameters"]
    errors: list[str] = []
    for item in data:
        if not isinstance(item, dict) or "ParameterKey" not in item or "ParameterValue" not in item:
            errors.append(f"{path}: entry missing ParameterKey/ParameterValue: {item!r}")
    return errors


def main() -> int:
    repo = pathlib.Path(__file__).resolve().parents[1]
    templates = [
        repo / "backend" / "template.yaml",
        repo / "infrastructure" / "cloudformation" / "monitoring.yaml",
    ]
    param_files = [
        repo / "infrastructure" / "environments" / env / "parameters.json"
        for env in ("dev", "qa", "prod")
    ]

    all_errors: list[str] = []
    for template in templates:
        if not template.exists():
            all_errors.append(f"missing template: {template}")
            continue
        errs = validate_template(template)
        all_errors.extend(errs)
        if not errs:
            print(f"OK   {template.relative_to(repo)}")

    root = repo / "backend" / "template.yaml"
    if root.exists():
        ref_errs = validate_nested_refs(root)
        all_errors.extend(ref_errs)
        if not ref_errs:
            print("OK   nested-stack TemplateURL references resolve")

    for params in param_files:
        if not params.exists():
            all_errors.append(f"missing parameter file: {params}")
            continue
        errs = validate_params(params)
        all_errors.extend(errs)
        if not errs:
            print(f"OK   {params.relative_to(repo)}")

    if all_errors:
        print("\nVALIDATION ERRORS:")
        for err in all_errors:
            print(f"  - {err}")
        return 1
    print("\nAll templates and parameter files are structurally valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
