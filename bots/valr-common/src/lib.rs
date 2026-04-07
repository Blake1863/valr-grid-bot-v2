/// valr-common — Shared VALR API primitives
///
/// Provides three things any strategy needs:
///   1. `ValrClient`  — authenticated REST client
///   2. `TradeStream` — real-time price feed via OB_L1_DIFF WebSocket
///   3. `AccountStream` — real-time order fill/status events via account WebSocket
///
/// Usage pattern:
/// ```
/// let creds = Credentials::load()?;
/// let client = ValrClient::new(creds.clone());
///
/// // Price feed — shared across the whole process
/// let price = TradeStream::spawn("SOLUSDTPERP");
/// let mid = price.mid().await?; // blocks until first tick received
///
/// // Fill events
/// let (account, mut fills) = AccountStream::spawn(creds);
/// while let Some(fill) = fills.recv().await { ... }
/// ```

pub mod client;
pub mod stream;
pub mod types;

pub use client::{Credentials, ValrClient};
pub use stream::{AccountStream, TradeStream};
pub use types::{Balance, FillEvent};
