texture CP2077DepthInput : DEPTH;

sampler CP2077DepthSampler
{
    Texture = CP2077DepthInput;
    AddressU = CLAMP;
    AddressV = CLAMP;
    MinFilter = POINT;
    MagFilter = POINT;
    MipFilter = POINT;
};

texture CP2077DepthExport
{
    Width = BUFFER_WIDTH;
    Height = BUFFER_HEIGHT;
    Format = R32F;
};

void CP2077DepthVS(uint vertex_id : SV_VertexID, out float4 position : SV_Position, out float2 texcoord : TEXCOORD0)
{
    texcoord.x = (vertex_id == 2) ? 2.0 : 0.0;
    texcoord.y = (vertex_id == 1) ? 2.0 : 0.0;
    position = float4(texcoord * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
}

float CP2077DepthPS(float4 position : SV_Position, float2 texcoord : TEXCOORD0) : SV_Target
{
    return tex2Dlod(CP2077DepthSampler, float4(texcoord, 0.0, 0.0)).x;
}

technique CP2077DepthCopy
{
    pass
    {
        VertexShader = CP2077DepthVS;
        PixelShader = CP2077DepthPS;
        RenderTarget = CP2077DepthExport;
    }
}
