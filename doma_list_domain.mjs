import fs from 'node:fs';
import { ethers } from 'ethers';
import { ApiClient, ListingHandler, OrderbookType } from '@doma-protocol/orderbook-sdk';

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

async function main() {
  const inputRaw = fs.readFileSync(0, 'utf8');
  const input = JSON.parse(inputRaw);

  const chainId = Number(requireField(input, 'chainId'));
  const rpcUrl = String(requireField(input, 'rpcUrl'));
  const privateKey = String(requireField(input, 'privateKey'));
  const contract = String(requireField(input, 'contract'));
  const tokenId = String(requireField(input, 'tokenId'));
  const priceRaw = String(requireField(input, 'priceRaw'));
  const currencyContractAddress = String(requireField(input, 'currencyContractAddress'));
  const durationMs = Number(requireField(input, 'durationMs'));
  const source = String(input.source || 'doma-swap-bot-public');
  const baseUrl = String(input.orderbookBaseUrl || 'https://api.doma.xyz').replace(/\/+$/, '');
  const apiKey = String(input.apiKey || '');
  const proxy = String(input.proxy || '');

  if (proxy) {
    process.env.HTTP_PROXY = proxy;
    process.env.HTTPS_PROXY = proxy;
  }

  const defaultHeaders = {};
  if (apiKey) {
    defaultHeaders['Api-Key'] = apiKey;
    defaultHeaders['x-api-key'] = apiKey;
  }

  const provider = new ethers.JsonRpcProvider(rpcUrl, { chainId, name: 'doma' });
  const signer = new ethers.Wallet(privateKey, provider);
  const apiClient = new ApiClient({
    baseUrl,
    defaultHeaders,
  });

  // Avoid SDK preflight API calls that are often blocked by proxy Squid 503.
  // The final create-listing POST still goes through Doma API with the proxy.
  apiClient.getSupportedCurrencies = async () => ({
    currencies: [
      {
        contractAddress: currencyContractAddress,
        symbol: 'USDC.E',
        decimals: 6,
      },
    ],
  });

  const config = {
    source,
    chains: [buildDomaChain(chainId, rpcUrl)],
    apiClientOptions: {
      baseUrl,
      defaultHeaders,
    },
  };
  const handler = new ListingHandler(
    config,
    apiClient,
    signer,
    `eip155:${chainId}`,
    emitProgress,
  );

  const result = await handler.execute({
      source,
      orderbook: OrderbookType.DOMA,
      cancelExisting: false,
      marketplaceFees: [],
      items: [
        {
          contract,
          tokenId,
          price: priceRaw,
          currencyContractAddress,
          duration: durationMs,
        },
      ],
  });

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
