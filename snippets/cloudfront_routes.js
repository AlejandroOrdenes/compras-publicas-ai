// CloudFront Function (viewer request): ajusta la dirección antes de ir al origen.
//  - /api/...  → la API en la Lambda, sin el prefijo /api (la API no lo conoce).
//  - /buscar/  → /buscar/index.html en S3 (S3 no sabe que una carpeta muestra su index.html).
function handler(event) {
  var request = event.request;
  var uri = request.uri;
  if (uri === '/api' || uri.startsWith('/api/')) {
    request.uri = uri.substring(4) || '/';
    return request;
  }
  if (uri.endsWith('/')) {
    request.uri = uri + 'index.html';
  } else if (uri.lastIndexOf('.') < uri.lastIndexOf('/')) {
    request.uri = uri + '/index.html';   // /buscar (sin barra ni extensión) → /buscar/index.html
  }
  return request;
}
