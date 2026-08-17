// Pure-helper tests for web-next/lib/select.ts.
//
// Run with: cd web-next && node --test tests/select.test.mjs

import test from "node:test";
import assert from "node:assert/strict";
import {
  SELECT_EMPTY_SENTINEL,
  decodeSelectValue,
  encodeSelectValue,
} from "../lib/select.ts";

test("encodeSelectValue maps the backend empty default to a Radix-safe value", () => {
  assert.equal(encodeSelectValue(""), SELECT_EMPTY_SENTINEL);
  assert.notEqual(SELECT_EMPTY_SENTINEL, "");
});

test("encodeSelectValue leaves missing values unset", () => {
  assert.equal(encodeSelectValue(undefined), undefined);
  assert.equal(encodeSelectValue(null), undefined);
});

test("encodeSelectValue preserves strings and stringifies scalar values", () => {
  assert.equal(encodeSelectValue("cinematic"), "cinematic");
  assert.equal(encodeSelectValue(0), "0");
  assert.equal(encodeSelectValue(false), "false");
});

test("decodeSelectValue restores only the empty default sentinel", () => {
  assert.equal(decodeSelectValue(SELECT_EMPTY_SENTINEL), "");
  assert.equal(decodeSelectValue("cinematic"), "cinematic");
});

test("empty and non-empty select values round-trip", () => {
  for (const value of ["", "cinematic", "0", "false"]) {
    assert.equal(decodeSelectValue(encodeSelectValue(value)), value);
  }
});
