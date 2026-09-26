// The one logo, served from public/brand/logo.svg (F05.2). Never inlined: swapping the
// file is the whole procedure. alt is empty because the text beside it names the product.
export default function Logo({ size }) {
  return <img src="/brand/logo.svg" alt="" className="logo" width={size} height={size} />;
}
