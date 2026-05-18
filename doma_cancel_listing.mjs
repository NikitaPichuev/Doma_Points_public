import fs from 'node:fs';
import { ethers } from 'ethers';
import { createDomaOrderbookClient } from '@doma-protocol/orderbook-sdk';

function requireField(input, name) {
  const value = input[name];
  if (value === undefined || value === null || String(value).trim() === '') {
    throw new Error(`Missing required field: ${name}`);
  }
  return value;
}

function buildDomaChain(chainId, rpcUrl) {
  return {
    id: Number(chainId),
    name: 'Doma',
    nativeCurrency: { name: 'Ether', symbol: 'ETH', decimals: 18 },
    rpcUrls: {
      default: { http: [rpcUrl] },
      public: { http: [rpcUrl] },
    },
    blockExplorers: {
      default: { name: 'Doma Explorer', url: 'https://explorer.doma.xyz' },
    },
  };
}

function emitProgress(steps) {
  const latest = steps[steps.length - 1];
  if (!latest) {
    return;
  }
  const hashes = (latest.txHashes || []).map((x) => x.txHash).join(',');
  console.error(
    JSON.stringify({
      type: 'progress',
      status: latest.status,
      action: latest.action,
      state: latest.progressState || '',
      tx_hashes: hashes,
      error: latest.error || '',
    }),
  );
}

async function cancelListing(input, proxy) {
  const chainId = Number(requireField(input, 'chainId'));
  const rpcUrl = String(requireField(input, 'rpcUrl'));
  const privateKey = String(requireField(input, 'privateKey'));
  const orderId = String(requireField(input, 'orderId'));
  const cancellationType = String(input.cancellationType || 'off-chain');
  const source = String(input.source || 'doma-swap-bot-public');
  const baseUrl = String(input.orderbookBaseUrl || 'https://api.doma.xyz').replace(/\/+$/, '');
  const apiKey = String(input.apiKey || '');

  const defaultHeaders = {};
  if (apiKey) {
    defaultHeaders['Api-Key'] = apiKey;
    defaultHeaders['x-api-key'] = apiKey;
  }

  const previousHttpProxy = process.env.HTTP_PROXY;
  const previousHttpsProxy = process.env.HTTPS_PROXY;
  if (proxy) {
    process.env.HTTP_PROXY = proxy;
    process.env.HTTPS_PROXY = proxy;
  } else {
    delete process.env.HTTP_PROXY;
    delete process.env.HTTPS_PROXY;
  }

  const provider = new ethers.JsonRpcProvider(rpcUrl, { chainId, name: 'doma' });
  const signer = new ethers.Wallet(privateKey, provider);
  const client = createDomaOrderbookClient({
    source,
    chains: [buildDomaChain(chainId, rpcUrl)],
    apiClientOptions: {
      baseUrl,
      defaultHeaders,
    },
  });

  try {
    return await client.cancelListing({
      params: {
        orderId,
        cancellationType,
      },
      signer,
      chainId: `eip155:${chainId}`,
      onProgress: emitProgress,
    });
  } finally {
    if (previousHttpProxy === undefined) {
      delete process.env.HTTP_PROXY;
    } else {
      process.env.HTTP_PROXY = previousHttpProxy;
    }
    if (previousHttpsProxy === undefined) {
      delete process.env.HTTPS_PROXY;
    } else {
      process.env.HTTPS_PROXY = previousHttpsProxy;
    }
  }
}

async function main() {
  const inputRaw = fs.readFileSync(0, 'utf8');
  const input = JSON.parse(inputRaw);
  const proxy = String(input.proxy || '');

  const result = await cancelListing(input, proxy);

  console.log(JSON.stringify({ ok: true, result }));
}

main().catch((error) => {
  const payload = {
    ok: false,
    error: error && error.stack ? error.stack : String(error),
  };
  console.error(JSON.stringify(payload));
  process.exit(1);
});
