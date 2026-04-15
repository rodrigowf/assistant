package com.assistant.peripheral.network

import android.content.Context
import android.net.wifi.WifiManager
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.URL

data class DiscoveredServer(
    val ip: String,
    val port: Int,
    val wsUrl: String = "ws://$ip:$port"
)

object NetworkScanner {
    private const val TAG = "NetworkScanner"
    private const val BACKEND_PORT = 8765
    private const val PROBE_PATH = "/api/sessions"
    private const val CONNECT_TIMEOUT_MS = 400

    /**
     * Scans the local subnet for running assistant backends.
     * Uses the device's WiFi IP to determine the subnet (e.g. 192.168.0.x).
     * Returns all discovered servers sorted by IP.
     */
    suspend fun scan(context: Context): List<DiscoveredServer> = withContext(Dispatchers.IO) {
        val subnet = getSubnet(context) ?: return@withContext emptyList()
        Log.d(TAG, "Scanning subnet $subnet.0/24 on port $BACKEND_PORT")

        val found = (1..254).map { i ->
            async {
                val ip = "$subnet.$i"
                if (probe(ip, BACKEND_PORT)) {
                    Log.d(TAG, "Found backend at $ip:$BACKEND_PORT")
                    DiscoveredServer(ip = ip, port = BACKEND_PORT)
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

    /** Returns true if the assistant backend is reachable at the given IP:port. */
    private fun probe(ip: String, port: Int): Boolean {
        return try {
            // First a fast TCP reachability check
            val addr = InetAddress.getByName(ip)
            if (!addr.isReachable(CONNECT_TIMEOUT_MS)) return false
            // Then an HTTP check to confirm it's our backend
            val url = URL("http://$ip:$port$PROBE_PATH")
            val conn = url.openConnection() as HttpURLConnection
            conn.connectTimeout = CONNECT_TIMEOUT_MS
            conn.readTimeout = CONNECT_TIMEOUT_MS
            conn.requestMethod = "GET"
            val code = conn.responseCode
            conn.disconnect()
            code in 200..299 || code == 404  // 404 means server is up but path may differ
        } catch (e: Exception) {
            false
        }
    }
}
