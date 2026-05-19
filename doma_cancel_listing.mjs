import fs from 'node:fs';
import { ethers } from 'ethers';
import { Seaport } from '@opensea/seaport-js';
import { ApiClient } from '@doma-protocol/orderbook-sdk';
import axios from 'axios';
import { HttpsProxyAgent } from 'https-proxy-agent';

function requireField(input, name) {
  const value = input[name];
  if (value === undefined || value === null || String(value).trim() === '') {
    throw new Error(`Missing required field: ${name}`);
  }
  return value;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function normalizeProxy(value) {
  const v = String(value || '').trim();
  if (!v) {
    return '';
  }
  if (v.includes('://')) {
    return v;
  }
  if (v.includes('@')) {
    const idx = v.lastIndexOf('@');
    const left = v.slice(0, idx);
    const right = v.slice(idx + 1);
    const leftHost = left.split(':').slice(0, -1).join(':');
    const rightHost = right.split(':').slice(0, -1).join(':');
    const leftLooksHost = leftHost.includes('.') || leftHost.toLowerCase() === 'localhost';
    const rightLooksHost = rightHost.includes('.') || rightHost.toLowerCase() === 'localhost';
    if (leftLooksHost && !rightLooksHost && left.includes(':') && right.includes(':')) {
      return `http://${right}@${left}`;
    }
  }
  return `http://${v}`;
}

function configureAxiosProxy(proxy) {
  const normalized = normalizeProxy(proxy);
  if (!normalized) {
    return '';
  }
  const agent = new HttpsProxyAgent(normalized);
  axios.defaults.proxy = false;
  axios.defaults.httpAgent = agent;
  axios.defaults.httpsAgent = agent;
  return normalized;
}

async function postCancelWithRetry(apiClient, payload) {
  let lastError;
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      return await apiClient.cancelListing(payload, { timeout: 25000 });
    } catch (error) {
      lastError = error;
      if (attempt < 2) {
        await sleep(1500 * attempt);
      }
    }
  }
  throw lastError;
}

async function cancelListingOffChainDirect({ signer, chainId, orderId, baseUrl, defaultHeaders }) {
  const seaport = new Seaport(signer, {
    balanceAndApprovalChecksOnOrderCreation: false,
  });
  const seaportAddress = await seaport.contract.getAddress();
  const numericChainId = Number(chainId);
  const signature = await signer.signTypedData(
    {
      name: 'Seaport',
      version: '1.6',
      chainId: numericChainId,
      verifyingContract: seaportAddress,
    },
    {
      OrderHash: [{ name: 'orderHash', type: 'bytes32' }],
    },
    {
      orderHash: orderId,
    },
  );

  const apiClient = new ApiClient({
    baseUrl,
    defaultHeaders,
  });

  await postCancelWithRetry(apiClient, {
    orderId,
    signature: {
      orderHash: orderId,
      signature,
    },
  });

  return {
    transactionHash: null,
    status: 'success',
    gasUsed: '0',
    gasPrice: '0',
    mode: 'off-chain-direct',
  };
}

async function cancelListing(input, proxy) {
  const chainId = Number(requireField(input, 'chainId'));
  const rpcUrl = String(requireField(input, 'rpcUrl'));
  const privateKey = String(requireField(input, 'privateKey'));
  const orderId = String(requireField(input, 'orderId'));
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

  try {
    // Doma UI cancels marketplace listings with an off-chain OrderHash signature.
    // Do the same directly: no SDK getListing/on-chain cancel path.
    return await cancelListingOffChainDirect({
      signer,
      chainId,
      orderId,
      baseUrl,
      defaultHeaders,
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
  const proxy = configureAxiosProxy(input.proxy || '');

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
