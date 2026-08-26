/* Signing in.
 *
 * Firebase Authentication, Google only. The whole of this file's job is to turn
 * a click into an ID token that other scripts can put on a fetch, and to keep
 * the nav honest about who is signed in.
 *
 * Two things worth knowing:
 *
 * 1. **The token is the credential, and it expires.** `getIdToken()` refreshes
 *    it when it is close to expiry, so callers must ask for one per request
 *    rather than caching the string. `RoadCleaner.auth.token()` does that.
 * 2. **Nothing here is a security boundary.** Hiding a button stops nobody. The
 *    real check is server-side in `web/auth.py`, on every route that matters.
 *    This file is about the page not lying to you.
 *
 * An ES module, so it is deferred by definition and `type="module"` is required
 * on the tag. It publishes `window.RoadCleaner.auth` for the plain scripts --
 * dashcam.js, incidents.js -- which are not modules and cannot import it.
 */

import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import {
  GoogleAuthProvider,
  getAuth,
  onAuthStateChanged,
  signInWithPopup,
  signOut,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";

const configElement = document.getElementById("firebase-config");
let config = null;
try {
  config = JSON.parse(configElement?.textContent || "null");
} catch {
  config = null;
}

// Everything below assumes there is a project to talk to. With FIREBASE_* unset
// the server renders `null` here, and the site behaves as it did before
// accounts existed: no control in the nav, and the pages that want an account
// say so themselves.
const ready = Boolean(config && config.apiKey);

const listeners = new Set();
let currentUser = null;
let resolved = false;

/* The public surface. Defined even when sign-in is not configured, so callers
   can ask `RoadCleaner.auth.enabled` instead of feature-detecting the object. */
const api = {
  enabled: ready,
  get user() {
    return currentUser;
  },
  /* Whether the first auth state has arrived. Before it has, "signed out" and
     "not known yet" look identical, and a page that treats them the same
     flashes a sign-in panel at somebody who is already signed in. */
  get resolved() {
    return resolved;
  },
  token: async () => null,
  signIn: async () => {},
  signOut: async () => {},
  /* Fires immediately with the current state if it is already known, so a
     listener registered late still hears about it. */
  onChange(fn) {
    listeners.add(fn);
    if (resolved) fn(currentUser);
    return () => listeners.delete(fn);
  },
  /* fetch() with the bearer token attached. The point of routing every
     authenticated call through here is that no caller has to remember the
     header shape, and none of them can accidentally send a stale token. */
  async fetch(url, options = {}) {
    const token = await api.token();
    const headers = new Headers(options.headers || {});
    if (token) headers.set("Authorization", `Bearer ${token}`);
    return fetch(url, { ...options, headers });
  },
};

window.RoadCleaner = window.RoadCleaner || {};
window.RoadCleaner.auth = api;

const control = document.querySelector("[data-auth-control]");
const signInButton = document.querySelector("[data-auth-signin]");
const signOutButton = document.querySelector("[data-auth-signout]");
const whoSlot = document.querySelector("[data-auth-who]");
const emailSlot = document.querySelector("[data-auth-email]");
const nameSlot = document.querySelector("[data-auth-name]");
const avatarSlot = document.querySelector("[data-auth-avatar]");
const initialSlot = document.querySelector("[data-auth-initial]");
const accountToggle = document.querySelector("[data-account-toggle]");
const accountMenu = document.querySelector("[data-account-menu]");
const authOnly = document.querySelectorAll("[data-auth-only]");

function paint(user) {
  if (control) control.hidden = false;
  if (signInButton) signInButton.hidden = Boolean(user);
  if (whoSlot) whoSlot.hidden = !user;
  authOnly.forEach((el) => {
    el.hidden = !user;
  });

  if (!user) {
    closeAccountMenu();
    return;
  }

  const email = user.email || user.uid || "";
  if (emailSlot) emailSlot.textContent = email;
  // Google usually supplies a display name; the address is the fallback, and
  // the line is dropped entirely rather than repeating the email twice.
  if (nameSlot) {
    nameSlot.textContent = user.displayName || "";
    nameSlot.hidden = !user.displayName;
  }
  // The letter shows through whenever the photo is missing or fails to load --
  // it sits behind the image rather than being swapped in, so there is no
  // moment where the chip is empty.
  if (initialSlot) initialSlot.textContent = (email[0] || "?").toUpperCase();
  if (avatarSlot) {
    if (user.photoURL) {
      avatarSlot.src = user.photoURL;
      avatarSlot.hidden = false;
      avatarSlot.onerror = () => {
        avatarSlot.hidden = true;
      };
    } else {
      avatarSlot.hidden = true;
    }
  }
}

/* --- the account menu ---------------------------------------------------
   A popover rather than an inline row. The address is the widest thing in the
   header and the only one whose length nobody controls, so rendering it inline
   let one long address set the width of the whole nav and wrap the row. */

function openAccountMenu() {
  if (!accountMenu) return;
  accountMenu.hidden = false;
  accountToggle?.setAttribute("aria-expanded", "true");
}

function closeAccountMenu() {
  if (!accountMenu) return;
  accountMenu.hidden = true;
  accountToggle?.setAttribute("aria-expanded", "false");
}

accountToggle?.addEventListener("click", (event) => {
  event.stopPropagation();
  if (accountMenu?.hidden) openAccountMenu();
  else closeAccountMenu();
});

// Anywhere else, or Escape. A menu that only closes via its own button is a
// menu people leave open by accident.
document.addEventListener("click", (event) => {
  if (accountMenu && !accountMenu.hidden && !event.target.closest(".account")) {
    closeAccountMenu();
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeAccountMenu();
});

function announce(user) {
  currentUser = user;
  resolved = true;
  paint(user);
  listeners.forEach((fn) => {
    try {
      fn(user);
    } catch (err) {
      // One page's listener throwing must not stop the others being told, or
      // half the page ends up rendering the wrong state.
      console.error("auth listener failed", err);
    }
  });
}

if (!ready) {
  // Configured-off is a supported state, not an error. Tell anyone listening
  // that the answer is "nobody", so pages stop waiting on a resolution that is
  // never coming.
  announce(null);
} else {
  const app = initializeApp(config);
  const auth = getAuth(app);

  api.token = async () => {
    const user = auth.currentUser;
    if (!user) return null;
    try {
      return await user.getIdToken();
    } catch (err) {
      console.error("Could not refresh the sign-in token", err);
      return null;
    }
  };

  api.signIn = async () => {
    const provider = new GoogleAuthProvider();
    // Forces the account chooser. Without it a second sign-in silently reuses
    // whichever Google account the browser saw last, which is wrong on a shared
    // phone -- and this is a phone-first page.
    provider.setCustomParameters({ prompt: "select_account" });
    await signInWithPopup(auth, provider);
  };

  api.signOut = async () => {
    await signOut(auth);
  };

  onAuthStateChanged(auth, announce);

  if (signInButton) {
    signInButton.addEventListener("click", async () => {
      signInButton.disabled = true;
      try {
        await api.signIn();
      } catch (err) {
        // A closed popup is somebody changing their mind, not a failure.
        if (err?.code !== "auth/popup-closed-by-user" && err?.code !== "auth/cancelled-popup-request") {
          console.error(err);
          window.alert(`Could not sign in: ${err?.message || err}`);
        }
      } finally {
        signInButton.disabled = false;
      }
    });
  }

  if (signOutButton) {
    signOutButton.addEventListener("click", () => api.signOut());
  }
}
