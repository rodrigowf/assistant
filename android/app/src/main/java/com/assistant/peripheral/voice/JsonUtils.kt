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

// The WS layer hands voice providers shallow maps where nested values
// may arrive either as fully-walked Map/List or as raw JSONObject /
// JSONArray (depending on which conversion path took them). These
// helpers read either shape transparently so parsers don't have to
// branch at every access.

/** Read a nested value by key from a Map or JSONObject. */
internal fun readNestedAny(value: Any?, key: String): Any? = when (value) {
    is Map<*, *> -> value[key]
    is JSONObject -> value.opt(key)
    else -> null
}

/** Read a string field from a Map or JSONObject (null if absent or empty). */
internal fun readNestedString(value: Any?, key: String): String? = when (value) {
    is JSONObject -> value.optString(key, "").ifEmpty { null }
    is Map<*, *> -> value[key] as? String
    else -> null
}

/** Read a boolean field from a Map or JSONObject (null if absent). */
internal fun readNestedBoolean(value: Any?, key: String): Boolean? = when (value) {
    is Map<*, *> -> value[key] as? Boolean
    is JSONObject -> if (value.has(key)) value.optBoolean(key, false) else null
    else -> null
}
