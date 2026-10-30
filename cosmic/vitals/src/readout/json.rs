// SPDX-License-Identifier: MIT
//! JSON schema 2 (spec §9): one compact object per readout, so `--watch --json` is NDJSON.

use super::data::Document;

/// The document as one line of JSON, no trailing newline.
pub fn render(doc: &Document) -> String {
    // The document holds only strings, numbers and options: serializing it cannot fail.
    serde_json::to_string(doc).unwrap_or_else(|e| format!("{{\"schema\":2,\"error\":\"{e}\"}}"))
}
