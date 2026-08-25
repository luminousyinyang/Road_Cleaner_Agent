/* The live send console.

   Mirrors drill.js, with one difference that runs through everything below: the
   run it drives has a side effect outside this process. A drill that fails
   wasted a minute; a send that half-fails leaves somebody unsure whether a
   message went out. So the outcome is read from `result.sent` -- the transport's
   own answer -- and never inferred from the job reaching "done".

   The section stays hidden until `/api/demo/send` says the deployment is
   configured. Offering a button that promises real mail and then failing on an
   unset variable is worse than not offering it. */
(function () {
  const section = document.querySelector("[data-demo-send]");
  if (!section) return;

  const form = document.getElementById("demo-form");
  const input = document.getElementById("demo-prompt");
  const run = document.getElementById("demo-run");
  const stages = document.getElementById("demo-stages");
  const out = document.getElementById("demo-out");
  const error = document.getElementById("demo-error");
  const frames = document.getElementById("demo-frames");
  const outcome = document.getElementById("demo-outcome");
  const POLL_MS = 1200;
  // Set from the readiness check below; decides whether the run renders a clip.
  let wantsVeo = false;

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /* Ask before showing anything. Three settings have to line up -- a recipient,
     that recipient being allowlisted, and SMTP -- and the server is the only
     thing that knows whether they do. */
  (async function reveal() {
    try {
      const response = await fetch("/api/demo/send");
      if (!response.ok) return;
      const ready = await response.json();
      if (!ready.ready) return;
      if (ready.recipient) {
        document.getElementById("demo-recipient").textContent = ready.recipient;
      }
      // Real footage or flat renders decides what the report encloses, so the
      // run asks for a clip only when one can actually be made.
      wantsVeo = Boolean(ready.veo);
      const note = document.getElementById("demo-footage");
      if (note) {
        note.textContent = wantsVeo
          ? "Veo generates the footage, and the stills the model reads — and the ones it encloses — are cut from that clip."
          : "MEDIA_PROVIDER is not set to vertex, so this run stages flat scene renders instead of real footage. The stills it encloses will look like diagrams.";
      }
      section.hidden = false;
    } catch {
      // Not configured, or the server is unhappy. Either way, no section.
    }
  })();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const prompt = input.value.trim();
    if (!prompt) return;

    run.disabled = true;
    run.textContent = "Running…";
    error.hidden = true;
    out.hidden = true;
    stages.hidden = false;
    outcome.textContent = "";
    outcome.className = "draft__why";
    resetStages();

    try {
      const started = await fetch("/api/demo/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, full: wantsVeo }),
      });
      if (!started.ok) {
        fail(await describe(started));
        return;
      }
      await follow((await started.json()).id);
    } catch (err) {
      fail((err && err.message) || String(err));
    } finally {
      run.disabled = false;
      run.textContent = "Run it and send";
    }
  });

  // Job state lives in the serving process, so a poll that lands on another
  // instance is told there is no such run -- truthfully, and about a run that is
  // still going elsewhere. Retried before it is believed; see inspect.js.
  const MISSES_ALLOWED = 5;

  async function follow(jobId) {
    let misses = 0;
    for (;;) {
      const response = await fetch(`/api/demo/send/${encodeURIComponent(jobId)}`);
      if (response.status === 404 && misses < MISSES_ALLOWED) {
        misses += 1;
        await wait(POLL_MS);
        continue;
      }
      if (!response.ok) {
        fail(await describe(response));
        return;
      }
      misses = 0;
      const job = await response.json();
      if (job.result) paint(job.result);

      if (job.state === "failed") {
        fail(job.error || "The run failed.");
        return;
      }
      if (job.state === "done") {
        report(job.result);
        return;
      }
      await wait(POLL_MS);
    }
  }

  function resetStages() {
    stages.querySelectorAll(".stage").forEach((item) => {
      item.dataset.state = "pending";
      item.querySelector(".stage__detail").textContent = "";
    });
  }

  function paint(result) {
    (result.stages || []).forEach((stage) => {
      const item = stages.querySelector(`[data-stage="${stage.key}"]`);
      if (!item) return;
      item.dataset.state = stage.state;
      item.querySelector(".stage__detail").textContent = stage.detail || "";
    });

    const drill = result.drill || {};
    if (drill.frame_urls && drill.frame_urls.length && !frames.childElementCount) {
      drill.frame_urls.forEach((url) => {
        const image = document.createElement("img");
        image.src = url;
        image.alt = "A staged frame from the run";
        image.className = "drill__frame";
        frames.append(image);
      });
    }
    if (drill.report_body) {
      document.getElementById("demo-case").textContent = drill.case_id || "";
      document.getElementById("demo-subject").textContent = drill.report_subject || "";
      document.getElementById("demo-body").textContent = drill.report_body;
      out.hidden = false;
    }
  }

  /* What actually happened to the message.

     `result.sent` is the transport's answer, not the job's. A run can finish
     cleanly and still not have sent -- SMTP refused, nothing was composed -- and
     saying "sent" because the job ended would be the one lie this whole section
     exists to disprove. */
  function report(result) {
    if (!result) return;
    const owner = result.would_have_gone_to || "no agency (the rules could not resolve one)";

    if (result.sent) {
      const stills = result.attachments || 0;
      const enclosure = stills
        ? `${stills} evidence still${stills === 1 ? "" : "s"} attached`
        : "no stills attached";
      outcome.className = "draft__why draft__why--sent";
      outcome.innerHTML =
        `<strong>Sent.</strong> Delivered to ${escapeHtml(result.sent_to)} over SMTP, ` +
        `${enclosure}. The jurisdiction rules resolved this stretch to ` +
        `${escapeHtml(owner)} — in normal operation that is where it would have ` +
        `gone, and the message says so.`;
      return;
    }

    // Held and failed are different outcomes and only one is a fault. The gate
    // declining is the product working -- saying "not sent" in the same breath
    // as an SMTP error would file both under "something went wrong".
    if (result.gate_decision) {
      outcome.className = "draft__why";
      outcome.innerHTML =
        `<strong>Held, not sent.</strong> The confidence gate returned ` +
        `<code>${escapeHtml(result.gate_decision)}</code>, so this never became a ` +
        `filing. ${escapeHtml(result.error || "")} That is the gate doing its job: ` +
        `two looks that disagree are not evidence, and the system would rather ` +
        `miss a hazard than send ${escapeHtml(owner)} after a shadow.`;
      return;
    }

    outcome.className = "draft__why";
    outcome.innerHTML =
      `<strong>Not sent.</strong> ${escapeHtml(result.error || "The transport refused it.")} ` +
      `The report above is real and was composed by the pipeline; only the ` +
      `handover failed.`;
  }

  function fail(message) {
    error.textContent = message;
    error.hidden = false;
  }

  async function describe(response) {
    try {
      const body = await response.json();
      return body.detail || `HTTP ${response.status}`;
    } catch {
      return `HTTP ${response.status}`;
    }
  }

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = String(value ?? "");
    return div.innerHTML;
  }
})();
