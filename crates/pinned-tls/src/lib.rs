//! TLS to an OpenPhalanx server. The server's certificate is self-signed, so
//! trust is a pinned SHA-256 fingerprint of that certificate instead of a CA:
//! hostnames and issuers are ignored, but the handshake signature is still
//! verified against the certificate's key, so a copied certificate is useless
//! without the server's private key.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{bail, Context, Result};
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{verify_tls12_signature, verify_tls13_signature, CryptoProvider};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{DigitallySignedStruct, SignatureScheme};
use sha2::{Digest, Sha256};

/// Marker in the TLS error, so callers can tell a pin mismatch from other failures.
const PIN_MISMATCH: &str = "openphalanx certificate fingerprint mismatch";

/// `AB:CD:…` SHA-256 of a DER certificate.
pub fn fingerprint(der: &[u8]) -> String {
    let digest = Sha256::digest(der);
    digest.iter().map(|b| format!("{b:02X}")).collect::<Vec<_>>().join(":")
}

/// The leading 8 bytes, as the OpenPhalanx app displays them.
pub fn short(fingerprint: &str) -> String {
    let groups: Vec<&str> = fingerprint.split(':').take(8).collect();
    format!("{}…", groups.join(":"))
}

/// Does `expected` (a full fingerprint, or a prefix of at least 8 bytes as
/// shown in the app, in any common notation) match `actual`?
pub fn matches(expected: &str, actual: &str) -> Result<bool> {
    let e = expected.trim();
    let e = e.strip_prefix("sha256:").or_else(|| e.strip_prefix("SHA256:")).unwrap_or(e);
    let hex: String = e.trim_end_matches('…').trim_end_matches("...").chars().filter(|c| *c != ':').collect();
    if hex.len() < 16 || hex.len() > 64 || !hex.len().is_multiple_of(2) || !hex.chars().all(|c| c.is_ascii_hexdigit()) {
        bail!("\"{expected}\" is not a fingerprint (need at least the first 8 bytes, e.g. F2:27:FE:3E:7D:85:17:E9)");
    }
    Ok(actual.replace(':', "").starts_with(&hex.to_ascii_uppercase()))
}

#[derive(Debug)]
enum Trust {
    /// Accept only this fingerprint.
    Pinned(String),
    /// Accept anything, but record what was presented (first contact only).
    Capture(Arc<Mutex<Option<String>>>),
}

#[derive(Debug)]
struct FingerprintVerifier {
    trust: Trust,
    provider: Arc<CryptoProvider>,
}

impl ServerCertVerifier for FingerprintVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        let presented = fingerprint(end_entity.as_ref());
        match &self.trust {
            Trust::Pinned(expected) if *expected == presented => Ok(ServerCertVerified::assertion()),
            Trust::Pinned(_) => Err(rustls::Error::General(format!("{PIN_MISMATCH}: server presented {presented}"))),
            Trust::Capture(slot) => {
                *slot.lock().unwrap() = Some(presented);
                Ok(ServerCertVerified::assertion())
            }
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls12_signature(message, cert, dss, &self.provider.signature_verification_algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(message, cert, dss, &self.provider.signature_verification_algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider.signature_verification_algorithms.supported_schemes()
    }
}

fn client(trust: Trust) -> Result<reqwest::Client> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let verifier = Arc::new(FingerprintVerifier { trust, provider: provider.clone() });
    let config = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .context("TLS setup failed")?
        .dangerous()
        .with_custom_certificate_verifier(verifier)
        .with_no_client_auth();
    reqwest::Client::builder()
        .use_preconfigured_tls(config)
        .connect_timeout(Duration::from_secs(10))
        .user_agent(concat!("oppx/", env!("CARGO_PKG_VERSION")))
        .build()
        .context("cannot build HTTPS client")
}

/// A client that only talks to the server whose certificate has `fingerprint`.
pub fn pinned_client(fingerprint: &str) -> Result<reqwest::Client> {
    client(Trust::Pinned(fingerprint.to_string()))
}

/// First contact: connect without trusting anything and return the
/// fingerprint of the certificate the server presented.
pub async fn probe_fingerprint(url: &str) -> Result<String> {
    let slot = Arc::new(Mutex::new(None));
    let c = client(Trust::Capture(slot.clone()))?;
    // The response itself doesn't matter; the handshake records the certificate.
    let result = c.get(format!("{url}/health")).timeout(Duration::from_secs(15)).send().await;
    let captured = slot.lock().unwrap().clone();
    match (captured, result) {
        (Some(fp), _) => Ok(fp),
        (None, Err(e)) => Err(anyhow::Error::new(e).context(format!("cannot reach {url}"))),
        (None, Ok(_)) => bail!("{url} did not present a TLS certificate"),
    }
}

/// True if `err` (or anything it wraps) is a pinned-fingerprint mismatch.
pub fn is_pin_mismatch(err: &(dyn std::error::Error + 'static)) -> bool {
    let mut cur: Option<&(dyn std::error::Error + 'static)> = Some(err);
    while let Some(e) = cur {
        if e.to_string().contains(PIN_MISMATCH) {
            return true;
        }
        cur = e.source();
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    const FP: &str = "F2:27:FE:3E:7D:85:17:E9:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:F2:27:FE:3E:7D:85:17:E9";

    #[test]
    fn formats_fingerprints() {
        let fp = fingerprint(b"not really a certificate");
        assert_eq!(fp.len(), 32 * 3 - 1);
        assert!(fp.chars().all(|c| c == ':' || c.is_ascii_hexdigit() && !c.is_ascii_lowercase()));
        assert_eq!(short(FP), "F2:27:FE:3E:7D:85:17:E9…");
    }

    #[test]
    fn matches_full_and_app_prefix() {
        assert!(matches(FP, FP).unwrap());
        assert!(matches(&FP.to_lowercase(), FP).unwrap());
        assert!(matches("F2:27:FE:3E:7D:85:17:E9…", FP).unwrap());
        assert!(matches("sha256:F227FE3E7D8517E9", FP).unwrap());
        assert!(!matches("F2:27:FE:3E:7D:85:17:E8", FP).unwrap());
        assert!(matches("F2:27:FE", FP).is_err(), "too short to be meaningful");
        assert!(matches("zz:27:FE:3E:7D:85:17:E9", FP).is_err());
    }

    #[test]
    fn detects_pin_mismatch_in_error_chain() {
        let inner = std::io::Error::other(format!("{PIN_MISMATCH}: server presented AA"));
        let outer = anyhow::Error::new(inner).context("request failed");
        assert!(is_pin_mismatch(outer.as_ref()));
        assert!(!is_pin_mismatch(anyhow::anyhow!("connection refused").as_ref()));
    }
}
