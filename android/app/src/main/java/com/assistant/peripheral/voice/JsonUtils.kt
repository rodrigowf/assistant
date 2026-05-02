package com.assistant.peripheral.voice

import org.json.JSONArray
import org.json.JSONObject

/**
 * Recursively convert a [JSONObject] to a `Map<String, Any?>` with
 * `JSONObject.NULL` collapsed to Kotlin `null`.  Used by voice
 * providers when forwarding raw events to the orchestrator WS layer.
 */
internal fun jsonObjectToMap(json: JSONObject): Map<String, Any?> {
    val map = mutableMapOf<String, Any?>()
    val keys = json.keys()
    while (keys.hasNext()) {
        val key = keys.next()
        val value = json.opt(key)
        map[key] = when (value) {
            is JSONObject -> jsonObjectToMap(value)
            is JSONArray -> jsonArrayToList(value)
            JSONObject.NULL -> null
            else -> value
        }
    }
    return map
}

internal fun jsonArrayToList(array: JSONArray): List<Any?> {
    val list = mutableListOf<Any?>()
    for (i in 0 until array.length()) {
        val value = array.opt(i)
        list.add(when (value) {
            is JSONObject -> jsonObjectToMap(value)
            is JSONArray -> jsonArrayToList(value)
            JSONObject.NULL -> null
            else -> value
        })
    }
    return list
}
