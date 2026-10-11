// Эталон правила комментария (план §3.3) на Swift и проверка на contract/vectors/comment.json.
//
//   swift contract/swift/comment_rule_check.swift contract/vectors/comment.json
//
// Та же логика, что Python-эталон в contract/tests/test_contract_vectors.py. iOS-реализация обязана
// давать те же результаты. Категории символов берутся из Unicode.Scalar.Properties.generalCategory,
// а не из CharacterSet: так результат не зависит от состава наборов Foundation.

import Foundation

func isZ(_ u: Unicode.Scalar) -> Bool {
    switch u.properties.generalCategory {
    case .spaceSeparator, .lineSeparator, .paragraphSeparator: return true
    default: return false
    }
}

/// NFC → удалить Cc → обрезать по краям Zs/Zl/Zp → пустая строка = nil.
func normalizeComment(_ s: String) -> String? {
    let nfc = s.precomposedStringWithCanonicalMapping
    var scalars = nfc.unicodeScalars.filter { $0.properties.generalCategory != .control }
    while let first = scalars.first, isZ(first) { scalars.removeFirst() }
    while let last = scalars.last, isZ(last) { scalars.removeLast() }
    var view = String.UnicodeScalarView()
    view.append(contentsOf: scalars)
    return view.isEmpty ? nil : String(view)
}

/// Наибольший префикс из целых графем (Character) длиной ≤ limit Unicode scalar values.
func truncateComment(_ s: String?, limit: Int = 200) -> String? {
    guard let s else { return nil }
    var out = String.UnicodeScalarView()
    for ch in s {
        if out.count + ch.unicodeScalars.count > limit { break }
        out.append(contentsOf: ch.unicodeScalars)
    }
    return out.isEmpty ? nil : String(out)
}

/// Вариант через CharacterSet — только для сравнения, не эталон.
func normalizeWithCharacterSet(_ s: String) -> String? {
    let nfc = s.precomposedStringWithCanonicalMapping
    var view = String.UnicodeScalarView()
    view.append(contentsOf: nfc.unicodeScalars.filter { $0.properties.generalCategory != .control })
    let out = String(view).trimmingCharacters(in: .whitespacesAndNewlines)
    return out.isEmpty ? nil : out
}

// Строки Swift сравниваются с учётом канонической эквивалентности: сравниваем скаляры.
func same(_ a: String?, _ b: String?) -> Bool {
    switch (a, b) {
    case (nil, nil): return true
    case let (x?, y?): return Array(x.unicodeScalars) == Array(y.unicodeScalars)
    default: return false
    }
}

// JSONDecoder, а не JSONSerialization: JSONSerialization теряет ведущий U+FEFF в строках.
struct Vector: Decodable {
    let id: String
    let input: String
    let normalized: String?
    let scalarCount: Int
    let syncValid: Bool
    let importTruncated: String?
}
struct Vectors: Decodable {
    let maxScalars: Int
    let cases: [Vector]
}

let path = CommandLine.arguments.dropFirst().first ?? "contract/vectors/comment.json"
let vectors = try JSONDecoder().decode(Vectors.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
let maxScalars = vectors.maxScalars
let cases = vectors.cases

var failures: [String] = []
var characterSetDiffs: [String] = []
for c in cases {
    let normalized = normalizeComment(c.input)
    if !same(normalized, c.normalized) { failures.append("\(c.id): normalized") }
    let count = normalized?.unicodeScalars.count ?? 0
    if count != c.scalarCount { failures.append("\(c.id): scalarCount \(count)") }
    if (count <= maxScalars) != c.syncValid { failures.append("\(c.id): syncValid") }
    if !same(truncateComment(normalized, limit: maxScalars), c.importTruncated) {
        failures.append("\(c.id): importTruncated")
    }
    if !same(normalizeWithCharacterSet(c.input), c.normalized) { characterSetDiffs.append(c.id) }
}

print("Swift \(cases.count) векторов, расхождений с эталоном: \(failures.count)")
if !characterSetDiffs.isEmpty {
    print("CharacterSet.whitespacesAndNewlines расходится на: \(characterSetDiffs.joined(separator: ", "))")
}
for f in failures { print("FAIL \(f)") }
exit(failures.isEmpty ? 0 : 1)
