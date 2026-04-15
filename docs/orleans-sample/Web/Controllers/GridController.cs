using Microsoft.AspNetCore.Mvc;
using My.Interfaces;

namespace Web.Controllers;

[ApiController]
[Route("api/[controller]")]
public class GridController : ControllerBase
{
    private readonly IClusterClient _clusterClient;
    private readonly ILogger<GridController> _logger;

    public GridController(IClusterClient clusterClient, ILogger<GridController> logger)
    {
        _clusterClient = clusterClient;
        _logger = logger;
    }

    [HttpGet("{key}")]
    public async Task<ActionResult<string[][]>> Get(string key, [FromQuery] int nx = 3, [FromQuery] int ny = 3)
    {
        try
        {
            if (nx <= 0 || ny <= 0)
            {
                return BadRequest("Grid dimensions must be positive integers");
            }

            if (nx > 100 || ny > 100)
            {
                return BadRequest("Grid dimensions are too large (max 100x100)");
            }

            var grid = _clusterClient.GetGrain<IGrid>(key);
            var gridData = await grid.Get(nx, ny);

            // _logger.LogInformation("Retrieved {Nx}x{Ny} grid for key {Key}", nx, ny, key);

            return Ok(gridData);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error retrieving grid for key {Key} with dimensions {Nx}x{Ny}", key, nx, ny);
            return StatusCode(500, "Internal server error");
        }
    }
}