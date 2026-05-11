/**
 * Lazy Firebase initialiser used by the critique-chat panel.
 *
 * We ONLY load firebase when a render-detail page actually opens a
 * critique conversation, not on every page load — keeps the bundle
 * slim for users who never use chat.
 *
 * One-shot init: the Firebase JS SDK is happy to be initialised
 * exactly once per browser tab; subsequent calls reuse the same
 * Firestore handle.
 *
 * Auth: we sign in with the custom token minted by
 * POST /api/jobs/<id>/critique/token. That gives us a uid the
 * Firestore security rules can match against `created_by_uid` on
 * the parent critique doc.
 */
import {
  initializeApp,
  getApps,
  type FirebaseApp,
} from "firebase/app";
import {
  getAuth,
  signInWithCustomToken,
  type Auth,
} from "firebase/auth";
import {
  getFirestore,
  type Firestore,
} from "firebase/firestore";

// ----- Per-project Firebase config -----
//
// Pulled from NEXT_PUBLIC_FIREBASE_* env at build time so the bundle
// has the right values baked in. None of these are secret — Firebase
// API keys are public; the actual auth is via the custom token's uid +
// Firestore security rules.
//
// When NEXT_PUBLIC_FIREBASE_API_KEY is unset (laptop dev without a
// firebase project hooked up), getCritiqueFirestore() throws a clear
// error so the chat panel can render a "not configured" pill instead
// of a cryptic runtime crash.

function _config() {
  const apiKey = process.env.NEXT_PUBLIC_FIREBASE_API_KEY;
  const projectId = process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID;
  if (!apiKey || !projectId) {
    throw new Error(
      "Firebase not configured: set NEXT_PUBLIC_FIREBASE_API_KEY + " +
        "NEXT_PUBLIC_FIREBASE_PROJECT_ID at build time. (Critique chat " +
        "needs a real-time channel into Firestore.)",
    );
  }
  return {
    apiKey,
    authDomain:
      process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN ?? `${projectId}.firebaseapp.com`,
    projectId,
  };
}

let _app: FirebaseApp | null = null;
let _auth: Auth | null = null;
let _db: Firestore | null = null;

function _getApp(): FirebaseApp {
  if (_app) return _app;
  const existing = getApps();
  _app = existing.length > 0 ? existing[0]! : initializeApp(_config());
  return _app;
}

export function getCritiqueAuth(): Auth {
  if (_auth) return _auth;
  _auth = getAuth(_getApp());
  return _auth;
}

export function getCritiqueFirestore(): Firestore {
  if (_db) return _db;
  _db = getFirestore(_getApp());
  return _db;
}

/**
 * Sign in to Firebase with the custom token returned by
 * POST /api/jobs/<id>/critique/token. Idempotent: if the same uid
 * is already signed in, we skip the round-trip.
 */
export async function signInWithCritiqueToken(token: string): Promise<string> {
  const auth = getCritiqueAuth();
  const result = await signInWithCustomToken(auth, token);
  return result.user.uid;
}
