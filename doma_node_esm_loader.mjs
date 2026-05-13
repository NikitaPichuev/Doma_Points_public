export async function resolve(specifier, context, nextResolve) {
  try {
    return await nextResolve(specifier, context);
  } catch (error) {
    const canRetry =
      error &&
      (error.code === 'ERR_MODULE_NOT_FOUND' || error.code === 'ERR_UNSUPPORTED_DIR_IMPORT') &&
      (specifier.startsWith('./') ||
        specifier.startsWith('../') ||
        specifier.startsWith('/') ||
        (specifier.includes('/') && !specifier.startsWith('node:'))) &&
      !specifier.endsWith('.js') &&
      !specifier.endsWith('.json') &&
      !specifier.endsWith('.node');

    if (!canRetry) {
      throw error;
    }

    try {
      return await nextResolve(`${specifier}.js`, context);
    } catch (_) {
      return nextResolve(`${specifier}/index.js`, context);
    }
  }
}
