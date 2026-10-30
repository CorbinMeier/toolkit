// SPDX-License-Identifier: MIT
//! Shared Claude plan-usage code for the COSMIC applet and `vitals`.

pub mod feed;
pub mod pace;
#[cfg(feature = "usage-text")]
pub mod parse;
pub mod reading;
pub mod readout;
pub mod words;
