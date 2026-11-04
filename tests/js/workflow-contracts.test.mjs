//
// tests/js/workflow-contracts.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import yaml from "js-yaml";

const workflow = yaml.load(readFileSync(new URL("../../.github/workflows/docker-build.yml", import.meta.url), "utf8"));
const ciWorkflow = yaml.load(readFileSync(new URL("../../.github/workflows/ci.yml", import.meta.url), "utf8"));

function stepIndex(steps, predicate, label) {
  const index = steps.findIndex(predicate);
  assert.notEqual(index, -1, `${label} step is missing`);
  return index;
}

function assertReleaseGates(document) {
  const buildSteps = document.jobs.build.steps;
  const purityIndex = stepIndex(
    buildSteps,
    (step) => step.name?.startsWith("Verify runtime image purity") && step.run?.includes("tools/verify-runtime-image.py layers"),
    "runtime image purity",
  );
  const purity = buildSteps[purityIndex];
  assert.equal(purity["continue-on-error"], undefined, "purity failure must fail the build");
  assert.equal(purity.if, undefined, "purity verification must not be conditional");
  assert.match(purity.run, /^\s*set -euo pipefail/m);
  assert.match(
    purity.run,
    /^\s*python3 tools\/verify-runtime-image\.py layers "\$archive"\s*$/m,
    "purity verifier must run without masking its exit code",
  );
  const pushIndex = stepIndex(buildSteps, (step) => step.name === "Log in to Docker Hub (push)", "registry push login");
  assert.ok(purityIndex < pushIndex, "runtime image purity must pass before registry push credentials are used");

  const manifest = document.jobs["create-manifest"];
  assert.deepEqual([...manifest.needs].sort(), ["build", "meta"], "manifest job must depend on metadata and all builds");
  assert.match(manifest.if, /needs\.build\.result\s*==\s*'success'/);
  const steps = manifest.steps;
  const candidateIndex = stepIndex(steps, (step) => step.name === "Create candidate index", "candidate index creation");
  const verifyIndex = stepIndex(
    steps,
    (step) => step.name === "Verify candidate index" && step.run?.includes("tools/verify-manifest.py verify manifest-candidate.json digests"),
    "candidate manifest verification",
  );
  const publishIndex = stepIndex(steps, (step) => step.name === "Publish version tag", "version publication");
  const verify = steps[verifyIndex];
  assert.equal(verify["continue-on-error"], undefined, "candidate verification failure must fail the manifest job");
  assert.match(verify.run, /^\s*set -euo pipefail/m);
  assert.ok(candidateIndex < verifyIndex && verifyIndex < publishIndex);
  assert.match(steps[publishIndex].run, /imagetools create --tag "\$\{IMAGE_NAME\}:\$\{VERSION\}" "\$\{IMAGE_NAME\}@\$\{INDEX_DIGEST\}"/);
  assert.match(steps[publishIndex].run, /verify manifest-version\.json digests/);
}

test("CI runs on every branch and supports manual dispatch", () => {
  assert.deepEqual(ciWorkflow.on.push.branches, ["**"]);
  assert.ok(Object.hasOwn(ciWorkflow.on, "workflow_dispatch"));
});

test("notification, wizard and SMTP browser suites are listed and run once with JUnit output", () => {
  const steps = ciWorkflow.jobs.browser.steps;
  const selected = steps.filter((step) => step.run?.includes("--grep '@notifications|@wizard|@smtp-ui'"));
  const listStep = selected.find((step) => step.run.includes("--list"));
  const runSteps = selected.filter((step) => !step.run.includes("--list"));

  assert.ok(listStep, "the expected browser test set must be listed in CI");
  assert.match(listStep.run, /--list --reporter=json/);
  for (const spec of ["notifications.spec.mjs", "wizard.spec.mjs", "smtp.spec.mjs"]) {
    assert.ok(listStep.run.includes(spec), `discovery must require ${spec}`);
  }
  assert.equal(runSteps.length, 1, "the three browser suites must execute in one Chromium run");
  assert.equal(runSteps[0].env.JUSTUP_LAYOUT_JUNIT, "${{ runner.temp }}/notification-wizard-smtp.xml");
});

test("source scan bypasses setup-trivy only after checksum-verifying the pinned release binary", () => {
  const job = ciWorkflow.jobs["source-scan"];
  const installIndex = stepIndex(job.steps, (step) => step.name === "Install checksum-verified Trivy", "verified Trivy install");
  const scanIndex = stepIndex(job.steps, (step) => step.name === "Trivy source scan", "Trivy source scan");
  const install = job.steps[installIndex];
  const scan = job.steps[scanIndex];

  assert.ok(installIndex < scanIndex, "verified binary must be installed before scanning");
  assert.match(install.run, /sha256sum --check --strict/);
  assert.match(install.run, /releases\/download\/v\$\{TRIVY_VERSION\}\/trivy_\$\{TRIVY_VERSION\}_Linux-64bit\.tar\.gz/);
  assert.match(job.env.TRIVY_SHA256, /^[a-f0-9]{64}$/);
  assert.equal(job.env.TRIVY_VERSION, "0.70.0");
  assert.equal(scan.with.version, "v0.70.0");
  assert.equal(scan.with["skip-setup-trivy"], "true");
});

test("release publication is gated by image purity and verified candidate manifests", () => {
  assert.doesNotThrow(() => assertReleaseGates(workflow));

  const withoutPurityGate = JSON.parse(JSON.stringify(workflow));
  withoutPurityGate.jobs.build.steps = withoutPurityGate.jobs.build.steps.filter(
    (step) => !step.name?.startsWith("Verify runtime image purity"),
  );
  assert.throws(() => assertReleaseGates(withoutPurityGate), /purity step is missing/);

  const maskedPurityFailure = JSON.parse(JSON.stringify(workflow));
  const purityStep = maskedPurityFailure.jobs.build.steps.find((step) => step.name?.startsWith("Verify runtime image purity"));
  purityStep.run = purityStep.run.replace(
    'python3 tools/verify-runtime-image.py layers "$archive"',
    'python3 tools/verify-runtime-image.py layers "$archive" || true',
  );
  assert.throws(() => assertReleaseGates(maskedPurityFailure), /purity verifier must run/);

  const manifestWithoutBuildDependency = JSON.parse(JSON.stringify(workflow));
  manifestWithoutBuildDependency.jobs["create-manifest"].needs = ["meta"];
  assert.throws(() => assertReleaseGates(manifestWithoutBuildDependency), /manifest job must depend/);
});

test("runtime image purity step fails closed when the layer verifier rejects the archive", () => {
  const step = workflow.jobs.build.steps.find((item) => item.name?.startsWith("Verify runtime image purity"));
  const temp = mkdtempSync(path.join(tmpdir(), "justup-review-fixes-"));
  const bin = path.join(temp, "bin");
  const calls = path.join(temp, "python-calls.txt");
  try {
    mkdirSync(bin);
    const docker = path.join(bin, "docker");
    const python = path.join(bin, "python3");
    writeFileSync(docker, [
      "#!/bin/sh",
      'if [ "$1" = "save" ]; then',
      '  [ "$3" = "-o" ] || exit 90',
      '  : > "$4"',
      "fi",
      "exit 0",
      "",
    ].join("\n"));
    writeFileSync(python, [
      "#!/bin/sh",
      'printf "%s\\n" "$*" >> "$PYTHON_CALLS"',
      'if [ "$1" = "tools/verify-runtime-image.py" ] && [ "$2" = "layers" ]; then exit 42; fi',
      "exit 0",
      "",
    ].join("\n"));
    chmodSync(docker, 0o755);
    chmodSync(python, 0o755);

    const result = spawnSync("bash", ["--noprofile", "--norc", "-euo", "pipefail", "-c", step.run], {
      cwd: path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../.."),
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}${path.delimiter}${process.env.PATH}`,
        ARCH: "amd64",
        TEST_IMAGE: "justup:test-amd64",
        PLATFORM: "linux/amd64",
        RUNNER_TEMP: temp,
        PYTHON_CALLS: calls,
      },
    });

    assert.equal(result.status, 42, `the verifier's exit status should propagate: ${result.stdout}\n${result.stderr}`);
    assert.match(readFileSync(calls, "utf8"), /tools\/verify-runtime-image\.py layers/);
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
});