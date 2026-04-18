package com.assistant.peripheral.ui.components.markdown

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString

/**
 * Lightweight regex-based syntax highlighter for code blocks.
 * Produces an AnnotatedString with colored spans matching the oneDark theme.
 * Only used on capable devices (checked by MdCapabilities).
 */

private data class TokenRule(val regex: Regex, val color: Color)

/**
 * Highlight source code for the given language.
 * Returns an AnnotatedString with colored spans.
 */
fun highlightCode(language: String, code: String): AnnotatedString {
    val rules = getRulesForLanguage(language.lowercase())
    if (rules.isEmpty()) {
        // Unknown language — return plain text
        return AnnotatedString(code, SpanStyle(color = MdColors.codeText))
    }

    return buildAnnotatedString {
        append(code)
        // Apply base color to entire string
        addStyle(SpanStyle(color = MdColors.codeText), 0, code.length)

        // Apply token colors (later rules override earlier for overlapping ranges)
        val applied = mutableListOf<IntRange>()
        for (rule in rules) {
            for (match in rule.regex.findAll(code)) {
                val range = match.range
                // Skip if this range overlaps with an already-applied range
                if (applied.any { it.first <= range.last && range.first <= it.last }) continue
                addStyle(SpanStyle(color = rule.color), range.first, range.last + 1)
                applied.add(range)
            }
        }
    }
}

// ── Language rules ───────────────────────────────────────────────────────────

private fun getRulesForLanguage(lang: String): List<TokenRule> {
    return when (lang) {
        "python", "py" -> pythonRules
        "javascript", "js", "jsx" -> jsRules
        "typescript", "ts", "tsx" -> tsRules
        "kotlin", "kt", "kts" -> kotlinRules
        "java" -> javaRules
        "bash", "sh", "shell", "zsh" -> bashRules
        "json" -> jsonRules
        "xml", "html", "svg" -> xmlRules
        "css", "scss" -> cssRules
        "go", "golang" -> goRules
        "rust", "rs" -> rustRules
        "sql" -> sqlRules
        "yaml", "yml" -> yamlRules
        else -> emptyList()
    }
}

// ── Comment/string patterns (reused across languages) ────────────────────────

private val cLineComment = TokenRule(Regex("//.*"), MdColors.synComment)
private val cBlockComment = TokenRule(Regex("/\\*[\\s\\S]*?\\*/"), MdColors.synComment)
private val hashComment = TokenRule(Regex("#.*"), MdColors.synComment)
private val doubleString = TokenRule(Regex("\"(?:[^\"\\\\]|\\\\.)*\""), MdColors.synString)
private val singleString = TokenRule(Regex("'(?:[^'\\\\]|\\\\.)*'"), MdColors.synString)
private val templateString = TokenRule(Regex("`(?:[^`\\\\]|\\\\.)*`"), MdColors.synString)
private val numberLiteral = TokenRule(Regex("\\b\\d+\\.?\\d*(?:[eE][+-]?\\d+)?\\b"), MdColors.synNumber)
private val hexLiteral = TokenRule(Regex("\\b0[xX][0-9a-fA-F]+\\b"), MdColors.synNumber)

// ── Python ───────────────────────────────────────────────────────────────────

private val pythonRules = listOf(
    hashComment,
    TokenRule(Regex("\"\"\"[\\s\\S]*?\"\"\""), MdColors.synString),
    TokenRule(Regex("'''[\\s\\S]*?'''"), MdColors.synString),
    doubleString,
    singleString,
    hexLiteral,
    numberLiteral,
    TokenRule(
        Regex("\\b(?:def|class|if|elif|else|for|while|return|import|from|as|with|try|except|finally|raise|yield|async|await|lambda|pass|break|continue|del|in|not|and|or|is|assert|global|nonlocal)\\b"),
        MdColors.synKeyword
    ),
    TokenRule(Regex("\\b(?:None|True|False)\\b"), MdColors.synKeyword),
    TokenRule(Regex("\\b(?:str|int|float|bool|list|dict|tuple|set|type|object|bytes|range)\\b"), MdColors.synType),
    TokenRule(Regex("\\b(?:print|len|range|enumerate|zip|map|filter|isinstance|super|open|sorted|reversed|any|all|min|max|abs|sum|round|input|format|getattr|setattr|hasattr)\\b"), MdColors.synFunction),
    TokenRule(Regex("\\b\\w+(?=\\s*\\()"), MdColors.synFunction),
    TokenRule(Regex("@\\w+"), MdColors.synKeyword),
)

// ── JavaScript ───────────────────────────────────────────────────────────────

private val jsRules = listOf(
    cLineComment,
    cBlockComment,
    templateString,
    doubleString,
    singleString,
    hexLiteral,
    numberLiteral,
    TokenRule(
        Regex("\\b(?:const|let|var|function|class|if|else|for|while|do|return|import|export|from|default|switch|case|break|continue|throw|try|catch|finally|new|delete|typeof|instanceof|void|in|of|async|await|yield|this|super|extends|static|get|set)\\b"),
        MdColors.synKeyword
    ),
    TokenRule(Regex("\\b(?:null|undefined|true|false|NaN|Infinity)\\b"), MdColors.synKeyword),
    TokenRule(Regex("\\b(?:Array|Object|String|Number|Boolean|Map|Set|Promise|Error|RegExp|Date|JSON|Math|console|document|window)\\b"), MdColors.synType),
    TokenRule(Regex("=>"), MdColors.synOperator),
    TokenRule(Regex("\\b\\w+(?=\\s*\\()"), MdColors.synFunction),
)

// ── TypeScript (extends JS) ──────────────────────────────────────────────────

private val tsRules = listOf(
    cLineComment,
    cBlockComment,
    templateString,
    doubleString,
    singleString,
    hexLiteral,
    numberLiteral,
    TokenRule(
        Regex("\\b(?:const|let|var|function|class|if|else|for|while|do|return|import|export|from|default|switch|case|break|continue|throw|try|catch|finally|new|delete|typeof|instanceof|void|in|of|async|await|yield|this|super|extends|static|get|set|interface|type|enum|implements|abstract|declare|namespace|module|as|keyof|readonly)\\b"),
        MdColors.synKeyword
    ),
    TokenRule(Regex("\\b(?:null|undefined|true|false|NaN|Infinity)\\b"), MdColors.synKeyword),
    TokenRule(Regex("\\b(?:string|number|boolean|any|void|never|unknown|object|Array|Object|Promise|Record|Partial|Required|Pick|Omit|Map|Set)\\b"), MdColors.synType),
    TokenRule(Regex("=>"), MdColors.synOperator),
    TokenRule(Regex("\\b\\w+(?=\\s*[(<])"), MdColors.synFunction),
)

// ── Kotlin ───────────────────────────────────────────────────────────────────

private val kotlinRules = listOf(
    cLineComment,
    cBlockComment,
    TokenRule(Regex("\"\"\"[\\s\\S]*?\"\"\""), MdColors.synString),
    doubleString,
    singleString,
    hexLiteral,
    numberLiteral,
    TokenRule(
        Regex("\\b(?:fun|val|var|class|object|interface|enum|sealed|data|abstract|open|override|private|protected|public|internal|companion|init|constructor|if|else|when|for|while|do|return|break|continue|throw|try|catch|finally|import|package|as|is|in|by|suspend|inline|crossinline|noinline|reified|typealias|annotation|lateinit|const|tailrec|operator|infix|vararg|out|where|get|set)\\b"),
        MdColors.synKeyword
    ),
    TokenRule(Regex("\\b(?:null|true|false|this|super|it)\\b"), MdColors.synKeyword),
    TokenRule(Regex("\\b(?:String|Int|Long|Float|Double|Boolean|Char|Byte|Short|Unit|Any|Nothing|List|Map|Set|Array|Pair|Triple|Sequence|Flow|StateFlow|MutableStateFlow|MutableList|MutableMap|MutableSet|Modifier|Color|Composable)\\b"), MdColors.synType),
    TokenRule(Regex("->"), MdColors.synOperator),
    TokenRule(Regex("@\\w+"), MdColors.synKeyword),
    TokenRule(Regex("\\b\\w+(?=\\s*[(<])"), MdColors.synFunction),
)

// ── Java ─────────────────────────────────────────────────────────────────────

private val javaRules = listOf(
    cLineComment,
    cBlockComment,
    doubleString,
    singleString,
    hexLiteral,
    numberLiteral,
    TokenRule(
        Regex("\\b(?:public|private|protected|static|final|abstract|class|interface|enum|extends|implements|new|return|if|else|for|while|do|switch|case|break|continue|throw|throws|try|catch|finally|import|package|void|this|super|instanceof|synchronized|volatile|transient|native|default)\\b"),
        MdColors.synKeyword
    ),
    TokenRule(Regex("\\b(?:null|true|false)\\b"), MdColors.synKeyword),
    TokenRule(Regex("\\b(?:String|int|long|float|double|boolean|char|byte|short|void|Integer|Long|Float|Double|Boolean|Object|List|Map|Set|Array|ArrayList|HashMap|Optional|Stream)\\b"), MdColors.synType),
    TokenRule(Regex("@\\w+"), MdColors.synKeyword),
    TokenRule(Regex("\\b\\w+(?=\\s*\\()"), MdColors.synFunction),
)

// ── Bash ─────────────────────────────────────────────────────────────────────

private val bashRules = listOf(
    hashComment,
    doubleString,
    singleString,
    hexLiteral,
    numberLiteral,
    TokenRule(
        Regex("\\b(?:if|then|else|elif|fi|for|do|done|while|until|case|esac|function|in|select|return|exit|break|continue|local|export|source|alias|unalias|declare|readonly|typeset|trap|eval|exec|set|unset|shift|getopts)\\b"),
        MdColors.synKeyword
    ),
    TokenRule(Regex("\\$\\{?\\w+\\}?"), MdColors.synType),
    TokenRule(Regex("\\b\\w+(?=\\s*\\()"), MdColors.synFunction),
)

// ── JSON ─────────────────────────────────────────────────────────────────────

private val jsonRules = listOf(
    // Keys (before general strings)
    TokenRule(Regex("\"[^\"]*\"(?=\\s*:)"), MdColors.synFunction),
    doubleString,
    numberLiteral,
    TokenRule(Regex("\\b(?:true|false|null)\\b"), MdColors.synKeyword),
)

// ── XML/HTML ─────────────────────────────────────────────────────────────────

private val xmlRules = listOf(
    TokenRule(Regex("<!--[\\s\\S]*?-->"), MdColors.synComment),
    doubleString,
    singleString,
    TokenRule(Regex("</?\\w[\\w-]*"), MdColors.synKeyword),
    TokenRule(Regex("/?>"), MdColors.synKeyword),
    TokenRule(Regex("\\b\\w+(?==)"), MdColors.synFunction),
)

// ── CSS ──────────────────────────────────────────────────────────────────────

private val cssRules = listOf(
    cBlockComment,
    doubleString,
    singleString,
    numberLiteral,
    TokenRule(Regex("#[0-9a-fA-F]{3,8}\\b"), MdColors.synNumber),
    TokenRule(Regex("\\b\\d+(?:px|em|rem|%|vh|vw|deg|s|ms)\\b"), MdColors.synNumber),
    TokenRule(Regex("[.#]\\w[\\w-]*"), MdColors.synKeyword),
    TokenRule(Regex("\\b(?:color|background|margin|padding|border|font|display|position|width|height|flex|grid|align|justify|transition|transform|opacity|overflow|z-index|cursor|content|box-shadow|text-decoration|letter-spacing|line-height)(?=[\\s-]*:)"), MdColors.synType),
    TokenRule(Regex("@\\w+"), MdColors.synKeyword),
    TokenRule(Regex("\\b\\w+(?=\\s*\\()"), MdColors.synFunction),
)

// ── Go ───────────────────────────────────────────────────────────────────────

private val goRules = listOf(
    cLineComment,
    cBlockComment,
    templateString,
    doubleString,
    singleString,
    hexLiteral,
    numberLiteral,
    TokenRule(
        Regex("\\b(?:func|var|const|type|struct|interface|map|chan|package|import|return|if|else|for|range|switch|case|default|break|continue|go|defer|select|fallthrough|goto)\\b"),
        MdColors.synKeyword
    ),
    TokenRule(Regex("\\b(?:nil|true|false|iota)\\b"), MdColors.synKeyword),
    TokenRule(Regex("\\b(?:string|int|int8|int16|int32|int64|uint|float32|float64|bool|byte|rune|error|any|complex64|complex128)\\b"), MdColors.synType),
    TokenRule(Regex(":=|<-"), MdColors.synOperator),
    TokenRule(Regex("\\b\\w+(?=\\s*\\()"), MdColors.synFunction),
)

// ── Rust ─────────────────────────────────────────────────────────────────────

private val rustRules = listOf(
    cLineComment,
    cBlockComment,
    doubleString,
    singleString,
    hexLiteral,
    numberLiteral,
    TokenRule(
        Regex("\\b(?:fn|let|mut|const|static|struct|enum|impl|trait|pub|mod|use|crate|self|super|return|if|else|for|while|loop|match|break|continue|move|ref|as|in|where|type|unsafe|extern|async|await|dyn|macro_rules)\\b"),
        MdColors.synKeyword
    ),
    TokenRule(Regex("\\b(?:true|false|None|Some|Ok|Err|Self)\\b"), MdColors.synKeyword),
    TokenRule(Regex("\\b(?:i8|i16|i32|i64|i128|u8|u16|u32|u64|u128|f32|f64|bool|char|str|String|Vec|Box|Rc|Arc|Option|Result|HashMap|HashSet|Iterator|Display|Debug|Clone|Copy|Send|Sync|Sized)\\b"), MdColors.synType),
    TokenRule(Regex("=>|->|::"), MdColors.synOperator),
    TokenRule(Regex("\\w+!"), MdColors.synFunction),
    TokenRule(Regex("\\b\\w+(?=\\s*[(<])"), MdColors.synFunction),
    TokenRule(Regex("#\\[\\w+"), MdColors.synKeyword),
)

// ── SQL ──────────────────────────────────────────────────────────────────────

private val sqlRules = listOf(
    TokenRule(Regex("--.*"), MdColors.synComment),
    cBlockComment,
    singleString,
    doubleString,
    numberLiteral,
    TokenRule(
        Regex("\\b(?:SELECT|FROM|WHERE|INSERT|INTO|UPDATE|SET|DELETE|CREATE|ALTER|DROP|TABLE|INDEX|VIEW|JOIN|LEFT|RIGHT|INNER|OUTER|CROSS|ON|AND|OR|NOT|IN|EXISTS|BETWEEN|LIKE|IS|NULL|AS|ORDER|BY|GROUP|HAVING|LIMIT|OFFSET|UNION|ALL|DISTINCT|CASE|WHEN|THEN|ELSE|END|BEGIN|COMMIT|ROLLBACK|GRANT|REVOKE|PRIMARY|KEY|FOREIGN|REFERENCES|CONSTRAINT|DEFAULT|CHECK|UNIQUE|VALUES|COUNT|SUM|AVG|MIN|MAX|COALESCE|CAST|NULLIF|IF)\\b", RegexOption.IGNORE_CASE),
        MdColors.synKeyword
    ),
    TokenRule(Regex("\\b(?:INT|INTEGER|VARCHAR|TEXT|BOOLEAN|DATE|TIMESTAMP|FLOAT|DOUBLE|DECIMAL|NUMERIC|CHAR|BLOB|SERIAL|BIGINT|SMALLINT)\\b", RegexOption.IGNORE_CASE), MdColors.synType),
    TokenRule(Regex("\\b\\w+(?=\\s*\\()"), MdColors.synFunction),
)

// ── YAML ─────────────────────────────────────────────────────────────────────

private val yamlRules = listOf(
    hashComment,
    doubleString,
    singleString,
    numberLiteral,
    TokenRule(Regex("^\\s*[\\w.-]+(?=\\s*:)", RegexOption.MULTILINE), MdColors.synFunction),
    TokenRule(Regex("\\b(?:true|false|null|yes|no|on|off)\\b", RegexOption.IGNORE_CASE), MdColors.synKeyword),
)
