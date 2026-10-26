(function(root) {
  "use strict";
  // A browser opt-in. Never include the operation key in URLs, exports or logs.
  class TokenPreference {
    constructor(storage=()=>root.localStorage) { this.storage=storage; this.key="taco-operation-token"; }
    read() {
      try { const token=this.storage().getItem(this.key);return {token:typeof token==="string" ? token.trim() : "",error:false}; }
      catch (_) { return {token:"",error:true}; }
    }
    save(token,remember) {
      try {
        if(remember && token)this.storage().setItem(this.key,token);
        else this.storage().removeItem(this.key);
        return {saved:Boolean(remember && token),error:false};
      } catch (_) { return {saved:false,error:true}; }
    }
  }
  root.TokenPreference=TokenPreference;
  if(typeof module!=="undefined" && module.exports)module.exports={TokenPreference};
})(typeof window!=="undefined" ? window : globalThis);
