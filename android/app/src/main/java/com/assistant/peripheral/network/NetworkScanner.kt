package com.assistant.peripheral.network

import android.content.Context
import android.net.wifi.WifiManager
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.withContext
import java.net.InetSocketAddress
import java.net.Socket

data class DiscoveredServer(
    val ip: String,
    val port: Int,
    val wsUrl: String = "ws://$ip:$port"
)

object NetworkScanner {
    private const val TAG = "NetworkScanner"
    private val PROBE_PORTS = listOf(80, 8765)
    private const val CONNECT_TIMEOUT_MS = 400

    /**
     * Scans the local subnet for running assistant backends.
     * Probes ports 80 (nginx) and 8765 (direct uvicorn), preferring 80.
     * Returns at most one entry per IP, sorted by IP.
     */
    suspend fun scan(context: Context): List<DiscoveredServer> = withContext(Dispatchers.IO) {
        val subnet = getSubnet(context) ?: return@withContext emptyList()
        Log.d(TAG, "Scanning subnet $subnet.0/24 on ports $PROBE_PORTS")

        val found = (2..254).map { i ->  // skip .1 (default gateway)
            async {
                val ip = "$subnet.$i"
                val port = PROBE_PORTS.firstOrNull { probe(ip, it) }
                if (port != null) {
                    Log.d(TAG, "Found backend at $ip:$port")
                    DiscoveredServer(ip = ip, port = port)
                } else null
            }
        }.awaitAll().filterNotNull()

        found.sortedBy { it.ip.split(".").last().toIntOrNull() ?: 0 }
    }

    /** Returns the first 3 octets of the device's WiFi IP, or null if unavailable. */
    private fun getSubnet(context: Context): String? {
        val wifiManager = context.applicationContext
            .getSystemService(Context.WIFI_SERVICE) as? WifiManager
            ?: return null
        val ipInt = wifiManager.connectionInfo?.ipAddress ?: return null
        if (ipInt == 0) return null
        // IpAddress is little-endian
        val a = ipInt and 0xFF
        val b = (ipInt shr 8) and 0xFF
        val c = (ipInt shr 16) and 0xFF
        return "$a.$b.$c"
    }

    /** Returns true if a TCP connection can be established to the given IP:port. */
    private fun probe(ip: String, port: Int): Boolean {
        return try {
            Socket().use { socket ->
                socket.connect(java.net.InetSocketAddress(ip, port), CONNECT_TIMEOUT_MS)
                true
            }
        } catch (e: Exception) {
            false
        }
    }
}
