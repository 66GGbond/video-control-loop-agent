/* Trial UI deliberately exposes allowances only, never device attribution. */
let trialStatus = null;
let trialVerificationPending = null;
const paidActions = new Set(["/api/create-project", "/api/confirm-context", "/api/revise-brief", "/api/generate-video", "/api/run-loop"]);

function updateTrial(trial, message) {
  if (!trial) return;
  trialStatus = trial;
  const status = document.querySelector("#trial-status");
  const button = document.querySelector("#trial-open");
  button.textContent = trial.authorized ? "更换体验码" : "输入体验码";
  if (trial.authorized) {
    status.textContent = message || (trial.reserved_seconds > 0
      ? `剩余视频额度：${trial.remaining_seconds} 秒 · 已预留 ${trial.reserved_seconds} 秒额度`
      : trial.remaining_seconds === 0
        ? "体验码的视频额度已用完，感谢体验。你仍可查看已生成的视频和演示案例。"
        : `剩余视频额度：${trial.remaining_seconds} 秒`);
  } else {
    status.textContent = trial.locked ? "体验码输入已锁定，请联系作品作者解锁。" : "输入体验码，解锁 AI 体验；也可直接查看演示案例。";
  }
}

function askTrialCode() {
  if (trialVerificationPending) return trialVerificationPending;
  const dialog = document.querySelector("#trial-dialog");
  const form = document.querySelector("#trial-form");
  const input = document.querySelector("#trial-code");
  const error = document.querySelector("#trial-error");
  const submit = document.querySelector("#trial-submit");
  input.value = "";
  error.textContent = trialStatus?.locked ? "体验码输入已锁定，请联系作品作者解锁。" : "";
  input.disabled = submit.disabled = Boolean(trialStatus?.locked);
  trialVerificationPending = new Promise(resolve => {
    let verified = false;
    form.onsubmit = async event => {
      event.preventDefault();
      if (submit.disabled) return;
      if (!input.value.trim()) { error.textContent = "请输入体验码。"; return; }
      submit.disabled = true;
      try {
        const response = await fetch("/api/trial/verify", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({code: input.value}) });
        const data = await response.json();
        updateTrial(data.trial);
        if (!response.ok) { error.textContent = data.error; return; }
        verified = true;
        updateTrial(data.trial, `体验码已启用，剩余视频额度 ${data.trial.remaining_seconds} 秒。`);
        dialog.close();
      } catch (_) {
        error.textContent = "暂时无法验证体验码，请检查网络后重试。";
      } finally {
        input.disabled = submit.disabled = Boolean(trialStatus?.locked);
      }
    };
    dialog.addEventListener("close", () => {
      form.onsubmit = null;
      trialVerificationPending = null;
      resolve(verified);
    }, {once: true});
  });
  dialog.showModal();
  input.focus();
  return trialVerificationPending;
}

async function confirmVideoQuota() {
  const state = renderedState;
  const seconds = Number(state?.creative_spec?.generation_spec?.duration_seconds);
  const remaining = trialStatus?.remaining_seconds || 0;
  if (trialStatus?.reserved_seconds > 0) throw new Error("已有视频生成中，请等待生成结果。");
  if (remaining === 0) throw new Error("视频额度已用完，暂时无法继续生成。");
  if (seconds > remaining) throw new Error(`剩余视频额度 ${remaining} 秒，不足以生成 ${seconds} 秒视频，请缩短生成时长。`);
  const repairing = state?.status === "awaiting_repair_upload";
  document.querySelector("#quota-confirm-copy").textContent = repairing
    ? `本次优化将重新生成 ${seconds} 秒视频，并使用对应额度，完成后剩余 ${remaining-seconds} 秒。`
    : `本次将使用 ${seconds} 秒视频额度，完成后剩余 ${remaining-seconds} 秒。`;
  const dialog = document.querySelector("#quota-confirm-dialog");
  document.querySelector("#quota-confirm-button").textContent = repairing ? "确认优化" : "开始生成";
  return new Promise(resolve => {
    dialog.returnValue = "cancel";
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once: true});
    dialog.showModal();
  });
}

async function trialRequest(path, options = {}, retry = false) {
  if (!retry && paidActions.has(path) && !trialStatus?.authorized) {
    if (!await askTrialCode()) throw new Error("已取消本次操作。");
  }
  if (!retry && path === "/api/generate-video" && !await confirmVideoQuota()) {
    if (renderedState?.execution_mode === "autopilot") {
      render(await trialRequest("/api/autopilot", {method: "POST", body: JSON.stringify({action: "pause"})}));
    }
    throw new Error("已取消本次生成。");
  }
  const response = await fetch(path, {headers: {"Content-Type": "application/json"}, ...options});
  const body = await response.json();
  updateTrial(body.trial);
  if (!response.ok) {
    if (body.code === "trial_required" && !retry && await askTrialCode()) return trialRequest(path, options, true);
    throw new Error(body.error || "请求失败，请稍后再试。");
  }
  if (path === "/api/generate-video") updateTrial(body.trial, `视频生成中，已预留 ${body.trial.reserved_seconds} 秒额度。剩余视频额度：${body.trial.remaining_seconds} 秒。`);
  if (path === "/api/demo") document.querySelector("#demo-notice").hidden = false;
  if (path === "/api/reset" || path === "/api/create-project") document.querySelector("#demo-notice").hidden = true;
  if (path === "/api/refresh-generation" && body.trial?.authorized) {
    if (body.status === "generated_video_ready") updateTrial(body.trial, `视频已生成，剩余视频额度 ${body.trial.remaining_seconds} 秒。${body.trial.remaining_seconds === 0 ? "体验码的视频额度已用完，感谢体验。" : ""}`);
    else if (body.status === "generating_video") updateTrial(body.trial, `视频生成中，已预留 ${body.trial.reserved_seconds} 秒额度。剩余视频额度：${body.trial.remaining_seconds} 秒。`);
    else if (body.version_reviews?.at(-1)?.status === "generation_failed") updateTrial(body.trial, `视频生成失败，剩余视频额度 ${body.trial.remaining_seconds} 秒。`);
  }
  if (body.trial_notice) updateTrial(body.trial, body.trial_notice);
  return body;
}

document.querySelector("#trial-open").addEventListener("click", askTrialCode);
document.querySelector("#trial-cancel").addEventListener("click", () => document.querySelector("#trial-dialog").close());
document.querySelector("#demo-open").addEventListener("click", async () => {
  try { render(await trialRequest("/api/demo", {method: "POST"})); }
  catch (error) { alert(error.message); }
});
