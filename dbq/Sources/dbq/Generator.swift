import CLlama
import Foundation

enum GeneratorError: Error, CustomStringConvertible {
    case modelLoad(String), context, template, tokenize, decode, grammar

    var description: String {
        switch self {
        case .modelLoad(let p): return "could not load the model at \(p)"
        case .context: return "could not create a llama.cpp context"
        case .template: return "the model has no usable chat template"
        case .tokenize: return "could not tokenize the prompt"
        case .decode: return "llama_decode failed"
        case .grammar: return "the grammar file could not be parsed"
        }
    }
}

/// Runs the fine-tuned GGUF with llama.cpp (Metal), greedy decoding, optionally grammar-constrained.
final class Generator {
    private let model: OpaquePointer
    private let ctx: OpaquePointer
    private let vocab: OpaquePointer
    private let template: String?

    init(modelPath: String, verbose: Bool) throws {
        if !verbose {
            llama_log_set({ _, _, _ in }, nil)
        }
        llama_backend_init()
        var mp = llama_model_default_params()
        mp.n_gpu_layers = 999
        guard let m = llama_model_load_from_file(modelPath, mp) else { throw GeneratorError.modelLoad(modelPath) }
        model = m
        var cp = llama_context_default_params()
        cp.n_ctx = 2048
        cp.n_batch = 2048
        cp.no_perf = true
        guard let c = llama_init_from_model(model, cp) else { throw GeneratorError.context }
        ctx = c
        vocab = llama_model_get_vocab(model)
        template = llama_model_chat_template(model, nil).map { String(cString: $0) }
    }

    deinit {
        llama_free(ctx)
        llama_model_free(model)
    }

    /// One chat turn per message pair: [(role, content)].
    func generate(messages: [(String, String)], grammar: String?, maxTokens: Int = 256) throws -> String {
        let prompt = try applyTemplate(messages)
        llama_memory_clear(llama_get_memory(ctx), true)

        var tokens = try tokenize(prompt)
        let chain = llama_sampler_chain_init(llama_sampler_chain_default_params())
        defer { llama_sampler_free(chain) }
        if let g = grammar {
            guard let gs = llama_sampler_init_grammar(vocab, g, "root") else { throw GeneratorError.grammar }
            llama_sampler_chain_add(chain, gs)
        }
        llama_sampler_chain_add(chain, llama_sampler_init_greedy())

        let n = Int32(tokens.count)
        let rc = tokens.withUnsafeMutableBufferPointer { llama_decode(ctx, llama_batch_get_one($0.baseAddress, n)) }
        guard rc == 0 else { throw GeneratorError.decode }

        var out = [UInt8]()
        var piece = [CChar](repeating: 0, count: 256)
        for _ in 0..<maxTokens {
            var tok = llama_sampler_sample(chain, ctx, -1)
            if llama_vocab_is_eog(vocab, tok) { break }
            let len = llama_token_to_piece(vocab, tok, &piece, Int32(piece.count), 0, false)
            if len > 0 { out.append(contentsOf: piece[0..<Int(len)].map { UInt8(bitPattern: $0) }) }
            guard llama_decode(ctx, llama_batch_get_one(&tok, 1)) == 0 else { throw GeneratorError.decode }
        }
        return String(decoding: out, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func applyTemplate(_ messages: [(String, String)]) throws -> String {
        // keep the C strings alive for the duration of the call
        let roles = messages.map { strdup($0.0)! }
        let contents = messages.map { strdup($0.1)! }
        defer { (roles + contents).forEach { free($0) } }
        var chat = zip(roles, contents).map { llama_chat_message(role: $0, content: $1) }
        var buf = [CChar](repeating: 0, count: 16384)
        let n = llama_chat_apply_template(template, &chat, chat.count, true, &buf, Int32(buf.count))
        guard n > 0, n < buf.count else { throw GeneratorError.template }
        return String(decoding: buf[0..<Int(n)].map { UInt8(bitPattern: $0) }, as: UTF8.self)
    }

    private func tokenize(_ text: String) throws -> [llama_token] {
        let len = Int32(text.utf8.count)
        let need = -llama_tokenize(vocab, text, len, nil, 0, true, true)
        guard need > 0 else { throw GeneratorError.tokenize }
        var tokens = [llama_token](repeating: 0, count: Int(need))
        let got = llama_tokenize(vocab, text, len, &tokens, need, true, true)
        guard got > 0 else { throw GeneratorError.tokenize }
        return Array(tokens[0..<Int(got)])
    }
}
