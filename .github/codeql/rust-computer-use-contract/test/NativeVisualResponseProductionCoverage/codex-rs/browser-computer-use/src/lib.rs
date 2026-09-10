struct Response;

type ProviderResult = Result<Response, String>;

fn handle_with_provider() -> ProviderResult {
    Err("no successful response exit".to_string())
}
