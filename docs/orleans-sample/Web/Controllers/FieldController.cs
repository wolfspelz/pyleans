using Microsoft.AspNetCore.Mvc;
using My.Interfaces;

namespace Web.Controllers;

[ApiController]
[Route("api/[controller]")]
public class FieldController : ControllerBase
{
    private readonly IClusterClient _clusterClient;
    private readonly ILogger<FieldController> _logger;

    public FieldController(IClusterClient clusterClient, ILogger<FieldController> logger)
    {
        _clusterClient = clusterClient;
        _logger = logger;
    }

    [HttpGet("{key}")]
    public async Task<ActionResult<FieldProperties>> Get(string key)
    {
        try
        {
            var field = _clusterClient.GetGrain<IField>(key);
            var value = await field.Get();

            // _logger.LogInformation("Retrieved field value for key {Key}: {Color} {Count}", key, value.Color, value.Count);

            return Ok(value);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error retrieving field for key {Key}", key);
            return StatusCode(500, "Internal server error");
        }
    }

    [HttpPost("{key}/select")]
    public async Task Select(string key, [FromBody] bool value)
    {
        try
        {
            var field = _clusterClient.GetGrain<IField>(key);
            await field.Select(value);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error selecting field for key {Key}", key);
        }
    }
}