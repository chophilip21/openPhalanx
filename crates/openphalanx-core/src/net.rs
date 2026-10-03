//! Network helpers.

use std::net::{IpAddr, TcpListener, UdpSocket};

/// The address other machines on the LAN reach this host on. Connecting a UDP
/// socket only selects a route; no packet is sent.
pub fn lan_ip() -> Option<IpAddr> {
    let socket = UdpSocket::bind("0.0.0.0:0").ok()?;
    socket.connect("192.0.2.1:80").ok()?;
    socket.local_addr().ok().map(|a| a.ip())
}

pub fn port_is_free(port: u16) -> bool {
    TcpListener::bind(("0.0.0.0", port)).is_ok()
}
