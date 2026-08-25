/* The two things you can do with a case.
 *
 * Full automation runs the real pipeline over the clip and mails the finished
 * report to whoever is signed in. It is the same Inspector the case page uses,
 * so the stages counting down here are the stages actually happening, not an
 * animation timed to look plausible.
 *
 * The other mode sends nothing. It asks who owns that road and hands back the
 * report to file by hand, which is the honest ending for the fifty-nine
 * agencies on file that publish a web form and no address at all.
 */

(function () {
  "use strict";

  const auth = window.RoadCleaner?.auth;
  const signinModal = document.getElementById("signin-modal");
  const handModal = document.getElementById("hand-modal");

  // Matches the drill's poll. Fast enough that boxes and stages feel live,
  // slow enough not to hammer the endpoint for a run that takes a minute.
  const POLL_MS = 1200;

  /* --- sign-in -------------------------------------------------------- */

  document.querySelector("[data-modal-signin]")?.addEventListener("click", async () => {
    try {
      await auth?.signIn();
      signinModal?.close();
    } catch (err) {
      if (err?.code !== "auth/popup-closed-by-user") {
        console.error(err);
      }
    }
  });

  document.querySelectorAll("[data-modal-close]").forEach((button) => {
    button.addEventListener("click", () => button.closest("dialog")?.close());
  });

  // Clicking the backdrop closes it. `<dialog>` fires click on the element
  // itself for backdrop presses, so the target being the dialog means outside.
  [signinModal, handModal].forEach((dialog) => {
    dialog?.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  });

  /* --- 1. full automation --------------------------------------------- */

  document.querySelectorAll("[data-run]").forEach(wireRun);

  function wireRun(panel) {
    const caseId = panel.dataset.run;
    const button = panel.querySelector(".run-go");
    const stages = panel.querySelector(".act__stages");
    const outcome = panel.querySelector(".act__outcome");
    const cost = panel.querySelector(".act__cost");

    button.addEventListener("click", async () => {
      // The run ends by sending mail, so it needs somewhere to send it. Asking
      // here rather than letting the 401 come back means the explanation
      // arrives before a minute of Vertex quota is spent, not after.
      if (!auth?.enabled || !auth.user) {
        signinModal?.showModal();
        return;
      }

      button.disabled = true;
      button.textContent = "Running…";
      outcome.hidden = true;
      stages.hidden = false;
      resetStages(stages);

      try {
        const response = await auth.fetch(`/api/cases/${caseId}/automate`, { method: "POST" });
        if (!response.ok) {
          fail(outcome, await describe(response));
          return;
        }
        const job = await response.json();
        await poll(job.id, stages, outcome, cost);
      } catch (err) {
        fail(outcome, `Could not start it: ${(err && err.message) || err}`);
      } finally {
        button.disabled = false;
        button.textContent = "Run it again";
      }
    });
  }

  async function poll(jobId, stages, outcome, cost) {
    for (;;) {
      await wait(POLL_MS);

      const response = await auth.fetch(`/api/inspect/${jobId}`);
      if (!response.ok) {
        fail(outcome, await describe(response));
        return;
      }
      const job = await response.json();
      if (job.result) paintStages(stages, job.result.stages || []);

      if (job.state === "failed") {
        fail(outcome, job.error || "The run failed.");
        return;
      }
      if (job.state === "done") {
        const sent = job.result?.sent_to;
        const gate = job.result?.gate_decision;
        outcome.hidden = false;
        outcome.classList.toggle("act__outcome--bad", !sent);
        if (sent) {
          outcome.textContent = `Sent to ${sent}. Nobody pressed send.`;
          if (cost) cost.textContent = "Check your inbox — the boxed still is attached.";
        } else if (gate && gate !== "file") {
          // Not a failure. The gate refusing is the product working, and a demo
          // that mails what the gate declined would be showing something false.
          outcome.textContent = `The gate said ${gate}, so nothing was sent.`;
        } else {
          outcome.textContent = "It finished, but the mail could not go out.";
        }
        return;
      }
    }
  }

  function resetStages(list) {
    list.querySelectorAll(".stage").forEach((li) => {
      li.className = "stage";
      const detail = li.querySelector(".stage__detail");
      if (detail) detail.textContent = "";
    });
  }

  function paintStages(list, reported) {
    reported.forEach((stage) => {
      const li = list.querySelector(`[data-stage="${stage.key}"]`);
      if (!li) return;
      li.className = `stage stage--${stage.state}`;
      const detail = li.querySelector(".stage__detail");
      if (detail) detail.textContent = stage.detail || "";
    });
  }

  /* --- 2. the handover ------------------------------------------------- */

  document.querySelectorAll("[data-hand]").forEach((panel) => {
    const caseId = panel.dataset.hand;
    const button = panel.querySelector(".hand-go");
    const outcome = panel.querySelector(".act__outcome");

    button.addEventListener("click", async () => {
      button.disabled = true;
      outcome.hidden = true;
      try {
        const response = await fetch(`/api/cases/${caseId}/handover`);
        if (!response.ok) {
          fail(outcome, await describe(response));
          return;
        }
        openHandover(await response.json());
      } catch (err) {
        fail(outcome, `Could not look that up: ${(err && err.message) || err}`);
      } finally {
        button.disabled = false;
      }
    });
  });

  function openHandover(data) {
    text("hand-case", `from ${data.case_id}`);
    text("hand-agency", data.agency);
    text("hand-where", data.location || "—");
    text("hand-rule", data.rule || "—");
    text("hand-subject", data.subject);
    text("hand-body", data.body);

    const open = document.getElementById("hand-open");

    // Branch on the channel the agency actually uses, not on which field
    // happens to be filled in. Several agencies publish both a form and a
    // contact address while routing reports through the form -- Georgia DOT is
    // one -- and offering a mail draft to those puts the report somewhere they
    // do not read it. `mailto:` with no address also opens nothing at all,
    // hence the second condition.
    if (data.channel === "email" && data.email) {
      text("hand-dest", data.email);
      text(
        "hand-how",
        `${data.agency} takes these by email. The draft below goes to them — ` +
          `send it in your own name and the reply comes back to you.`
      );
      open.textContent = "Open a draft";
      open.href =
        `mailto:${encodeURIComponent(data.email).replace(/%40/g, "@")}` +
        `?subject=${encodeURIComponent(data.subject)}&body=${encodeURIComponent(data.body)}`;
      open.removeAttribute("target");
    } else if (data.endpoint) {
      // Falls here for an agency that publishes an address but takes reports by
      // form. The address is still shown -- it is public and it is the honest
      // answer to "who is this" -- but the button goes where the report is
      // actually read.
      text("hand-dest", data.endpoint);
      text(
        "hand-how",
        `${data.agency} takes these by web form, not email — which is why this ` +
          `half cannot be automated. Copy the report, open the form, paste it in.`
      );
      open.textContent = "Open the form";
      open.href = data.endpoint;
      open.target = "_blank";
    } else {
      text("hand-dest", "nothing published");
      text(
        "hand-how",
        `${data.agency} owns that stretch but publishes no reporting address ` +
          `on file here. The report is below; there is nowhere for it to go.`
      );
      open.textContent = "No address";
      open.removeAttribute("href");
    }

    const copy = document.getElementById("hand-copy");
    copy.textContent = "Copy the report";
    copy.onclick = async () => {
      try {
        await navigator.clipboard.writeText(`${data.subject}\n\n${data.body}`);
        copy.textContent = "Copied";
      } catch {
        // Insecure context or permission refused. The text is on screen and
        // selectable, so say that rather than pretending it worked.
        copy.textContent = "Select it above";
      }
    };

    handModal?.showModal();
  }

  /* --- shared --------------------------------------------------------- */

  function text(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value || "";
  }

  function fail(slot, message) {
    if (!slot) return;
    slot.hidden = false;
    slot.classList.add("act__outcome--bad");
    slot.textContent = message;
  }

  function wait(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function describe(response) {
    try {
      const data = await response.json();
      return data.detail || `That failed (${response.status}).`;
    } catch {
      return `That failed (${response.status}).`;
    }
  }
})();
